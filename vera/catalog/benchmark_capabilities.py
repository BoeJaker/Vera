"""
benchmark_capabilities.py — Ollama model benchmarking (role-aware)
==================================================================
Add to _module_files in capability_orchestration.py (after the catalog):
    os.path.join(_here, "catalog/benchmark_capabilities.py"),

Answers one question the catalog could not: **how good is THIS model, on THIS
node, at THIS role — and how does it compare to the alternatives?** Everything
here is *specific to Ollama usage*: it talks to a node's own /api/generate,
/api/embeddings and /api/ps directly (never the router), so a measurement is
always attributed to the exact (instance, model) pair you picked.

Three families of metric, all read straight off Ollama's response envelope so
they are real, not modelled:

  • **Speed / token metrics** — prompt-eval tokens/sec, generation tokens/sec,
    an approximate time-to-first-token and total wall time (from the
    prompt_eval_/eval_/total_duration fields Ollama returns per call).
  • **Load time** — the cold model-load duration (Ollama's load_duration). With
    ``cold=True`` the model is unloaded first (keep_alive:0) so the figure is a
    true cold start rather than "already resident".
  • **Deterministic accuracy** — fixed, hand-checked ROLE PACKS (instruction-
    following, reasoning/math, code-reasoning, JSON/structured, factual QA, plus
    embeddings-retrieval MRR and a vision colour probe). Scored programmatically
    (exact / regex / contains / numeric / json), temperature 0 + fixed seed — no
    LLM judge, fully repeatable.

Results persist (Redis), so ``bench.compare`` can render a role leaderboard
across every model/node you have measured. ``bench.loop`` is the opt-in
"loop-lab as one mechanism" hook — it pins the chosen model onto its node and
runs it through a real agentic loop profile (loops.run) for a qualitative,
agentic read alongside the deterministic score.

Storage (Redis):
    vera:bench:results          list of compact result records (newest first)
    vera:bench:result:<id>      full result detail (TTL 30d)
    vera:bench:runs             hash run_id -> live status (background runs)
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import re
import struct
import sys
import time
import uuid
import zlib
from collections import deque
from typing import Any, Deque, Dict, List, Optional

import httpx

from Vera.vera.capability_orchestration import (
    capability, emit_event, enum_schema, now_iso, schedule,
)
import Vera.vera.capability_orchestration as _orch

log = logging.getLogger("vera.bench")

KEY_RESULTS = "vera:bench:results"       # list, newest-first, capped
KEY_RESULT  = "vera:bench:result:"       # + result_id
KEY_RUNS    = "vera:bench:runs"          # hash run_id -> status JSON
RESULTS_CAP = 300
RESULT_TTL  = 30 * 86400


# ─────────────────────────────────────────────────────────────────────────────
# Plumbing
# ─────────────────────────────────────────────────────────────────────────────
def _redis():
    return getattr(_orch, "REDIS", None)


def _ssl():
    return getattr(_orch, "_SSL_CTX", None) or True


def _rawcap(name: str):
    c = _orch.CAPABILITY_REGISTRY.get(name)
    return (c.get("raw") or c.get("func")) if c else None


def _instance(instance_id: str) -> Optional[dict]:
    return (getattr(_orch, "OLLAMA_INSTANCES", {}) or {}).get(instance_id)


def _instance_url(instance_id: str) -> str:
    return (_instance(instance_id) or {}).get("url", "") or ""


# ─────────────────────────────────────────────────────────────────────────────
# PASSIVE OBSERVATION  — read the router's rolling per-request statistics, which
# every real ollama_generate call already feeds. This is "benchmarking" from
# production traffic: no test is executed, so there is no accuracy — only real
# observed throughput, latency and usage per (model, node).
# ─────────────────────────────────────────────────────────────────────────────
def _route_stats() -> Dict[str, dict]:
    return getattr(_orch, "_ROUTE_STATS", {}) or {}


def _observed_tps(model: str, instance_id: str) -> Optional[float]:
    """Best observed tokens/sec for a model on a node from live traffic (any job)."""
    best = 0.0
    for s in _route_stats().values():
        if s.get("model") == model and s.get("instance") == instance_id:
            best = max(best, float(s.get("ema_tps") or 0.0))
    return round(best, 1) if best > 0 else None


def _passive_rows(instance_id: str = "") -> List[dict]:
    """Aggregate rolling route stats into one row per (model, node): observed
    tps, avg latency, request count and the job types seen."""
    agg: Dict[str, dict] = {}
    for s in _route_stats().values():
        model, iid = s.get("model", "?"), s.get("instance", "?")
        if instance_id and iid != instance_id:
            continue
        if not model or model == "?":
            continue
        key = f"{model}@{iid}"
        row = agg.setdefault(key, {
            "model": model, "instance_id": iid,
            "node_label": (_instance(iid) or {}).get("label", iid),
            "has_gpu": bool((_instance(iid) or {}).get("has_gpu")),
            "calls": 0, "job_types": set(), "_tps": [], "_elapsed": [],
            "_prompt_chars": [], "last_ts": ""})
        row["calls"] += int(s.get("n") or 0)
        if s.get("job_type"):
            row["job_types"].add(s["job_type"])
        if s.get("ema_tps"):
            row["_tps"].append(float(s["ema_tps"]))
        if s.get("ema_elapsed_s"):
            row["_elapsed"].append(float(s["ema_elapsed_s"]))
        if s.get("ema_prompt_chars"):
            row["_prompt_chars"].append(float(s["ema_prompt_chars"]))
        if s.get("last_ts", "") > row["last_ts"]:
            row["last_ts"] = s["last_ts"]
    out = []
    for row in agg.values():
        tps = row.pop("_tps"); el = row.pop("_elapsed"); pc = row.pop("_prompt_chars")
        row["obs_tps"] = round(max(tps), 1) if tps else None
        row["avg_latency_s"] = round(sum(el) / len(el), 2) if el else None
        row["avg_prompt_chars"] = round(sum(pc) / len(pc)) if pc else None
        row["job_types"] = sorted(row["job_types"])
        out.append(row)
    out.sort(key=lambda r: -(r.get("calls") or 0))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# DETERMINISTIC ROLE PACKS
# ─────────────────────────────────────────────────────────────────────────────
# Each item: {prompt, match:{type,...}, [num_predict]}. Matchers are checked
# against the model's stripped output. Kept deliberately small so a full run is
# a handful of seconds, and phrased to force a terse answer so scoring is clean.
#
# match types:
#   exact    value            — normalised output == value (case-insensitive)
#   contains value            — value in normalised output (case-insensitive)
#   regex    pattern          — re.search(pattern, output)  (IGNORECASE)
#   numeric  value [tol]      — last number in output == value (± tol)
#   json_eq  value            — output parses as JSON equal to value
#   json_has {k:v,...}        — output parses as JSON dict containing these pairs
#   json_valid                — output parses as JSON

ROLE_PACKS: Dict[str, dict] = {
    "instruct": {
        "label": "Instruction following",
        "blurb": "Terse, literal compliance — the base competence every role needs.",
        "kind": "generate",
        "items": [
            {"prompt": "Reply with exactly the word BANANA in uppercase and nothing else.",
             "match": {"type": "regex", "pattern": r"\bBANANA\b"}},
            {"prompt": "Respond with only the number of letters in the word 'strawberry'.",
             "match": {"type": "numeric", "value": 10}},
            {"prompt": "Output only the third word of this sentence: The quick brown fox jumps.",
             "match": {"type": "contains", "value": "brown"}},
            {"prompt": "Reply with only 'yes' or 'no': Is 17 a prime number?",
             "match": {"type": "regex", "pattern": r"^\W*yes\b"}},
            {"prompt": "Repeat this string exactly and output nothing else: alpha-bravo-charlie",
             "match": {"type": "contains", "value": "alpha-bravo-charlie"}},
            {"prompt": "Reply with only the uppercase first letter of the word 'vera'.",
             "match": {"type": "regex", "pattern": r"\bV\b"}},
        ],
    },
    "reasoning": {
        "label": "Reasoning · math",
        "blurb": "Grade-school word problems with a single exact numeric answer.",
        "kind": "generate",
        "num_predict": 256,
        "items": [
            {"prompt": "A shop has 3 boxes of 12 apples and sells 7 apples. How many apples "
                       "remain? Think briefly, then end with 'Answer: <number>'.",
             "match": {"type": "numeric", "value": 29}},
            {"prompt": "A train travels 60 km in 45 minutes. What is its speed in km/h? "
                       "End with 'Answer: <number>'.",
             "match": {"type": "numeric", "value": 80}},
            {"prompt": "What is 15% of 240? End with 'Answer: <number>'.",
             "match": {"type": "numeric", "value": 36}},
            {"prompt": "I have 5 apples, eat 2, buy 4 more, then give away 3. How many apples "
                       "do I have now? End with 'Answer: <number>'.",
             "match": {"type": "numeric", "value": 4}},
            {"prompt": "A number doubled and increased by 3 equals 17. What is the number? "
                       "End with 'Answer: <number>'.",
             "match": {"type": "numeric", "value": 7}},
            {"prompt": "How many days are in January, February (non-leap) and March combined? "
                       "End with 'Answer: <number>'.",
             "match": {"type": "numeric", "value": 90}},
        ],
    },
    "code": {
        "label": "Code reasoning",
        "blurb": "Predict the output of small Python snippets — deterministic, no sandbox.",
        "kind": "generate",
        "items": [
            {"prompt": "What does this Python print? print(len('hello world')) — output only the number.",
             "match": {"type": "numeric", "value": 11}},
            {"prompt": "What is the output of print(2 ** 10) in Python? Output only the number.",
             "match": {"type": "numeric", "value": 1024}},
            {"prompt": "In Python, print(sorted([3,1,2])) outputs what? Reply with only the list "
                       "in the form [1, 2, 3].",
             "match": {"type": "regex", "pattern": r"\[\s*1\s*,\s*2\s*,\s*3\s*\]"}},
            {"prompt": "Output only the result of the Python expression: 'ab' * 3",
             "match": {"type": "contains", "value": "ababab"}},
            {"prompt": "What is [i*i for i in range(4)] in Python? Reply with only the list.",
             "match": {"type": "regex", "pattern": r"\[\s*0\s*,\s*1\s*,\s*4\s*,\s*9\s*\]"}},
            {"prompt": "print(bin(5)) outputs what in Python? Reply with only the string "
                       "(for example 0b101).",
             "match": {"type": "contains", "value": "0b101"}},
        ],
    },
    "json": {
        "label": "JSON · structured",
        "blurb": "Emit strictly-valid JSON with the requested shape — the tool-use base skill.",
        "kind": "generate",
        "items": [
            {"prompt": "Return a JSON object with keys \"name\" (string \"vera\") and \"n\" "
                       "(number 42) and nothing else.",
             "match": {"type": "json_has", "value": {"name": "vera", "n": 42}}},
            {"prompt": "Output a JSON array of the first three positive even numbers and nothing else.",
             "match": {"type": "json_eq", "value": [2, 4, 6]}},
            {"prompt": "Return ONLY valid JSON: {\"ok\": true}. No explanation, no code fence.",
             "match": {"type": "json_has", "value": {"ok": True}}},
            {"prompt": "Return a JSON object mapping the key \"red\" to its hex code \"#ff0000\". "
                       "Only JSON.",
             "match": {"type": "json_has", "value": {"red": "#ff0000"}}},
            {"prompt": "Return a JSON object with a key \"items\" whose value is the list "
                       "[\"a\",\"b\"]. Only JSON.",
             "match": {"type": "json_has", "value": {"items": ["a", "b"]}}},
        ],
    },
    "factual": {
        "label": "Factual recall",
        "blurb": "Short, unambiguous world-knowledge questions.",
        "kind": "generate",
        "items": [
            {"prompt": "What is the capital of France? Answer in one word.",
             "match": {"type": "contains", "value": "paris"}},
            {"prompt": "What is the chemical symbol for gold? Reply with only the symbol.",
             "match": {"type": "regex", "pattern": r"\bAu\b"}},
            {"prompt": "How many continents are there on Earth? Reply with only the number.",
             "match": {"type": "numeric", "value": 7}},
            {"prompt": "Which planet is known as the Red Planet? Answer in one word.",
             "match": {"type": "contains", "value": "mars"}},
            {"prompt": "In what year did the first humans land on the Moon? Reply with only the year.",
             "match": {"type": "numeric", "value": 1969}},
            {"prompt": "What is the largest ocean on Earth? Answer with only its name.",
             "match": {"type": "contains", "value": "pacific"}},
        ],
    },
    "embed": {
        "label": "Embeddings · retrieval",
        "blurb": "Mean-reciprocal-rank of an embedding model over a fixed mini-corpus.",
        "kind": "embed",
    },
    "vision": {
        "label": "Vision · multimodal",
        "blurb": "Colour-identification probe for VLM nodes (synthetic images).",
        "kind": "vision",
        "items": [
            {"color": (220, 20, 20), "prompt": "What is the dominant colour of this image? "
                                                "Answer in one word.",
             "match": {"type": "contains", "value": "red"}},
            {"color": (20, 180, 40), "prompt": "What is the dominant colour of this image? "
                                                "Answer in one word.",
             "match": {"type": "contains", "value": "green"}},
            {"color": (30, 60, 220), "prompt": "What is the dominant colour of this image? "
                                                "Answer in one word.",
             "match": {"type": "contains", "value": "blue"}},
        ],
    },
}

# Roles that run on any generative model — the default "role: all" bundle.
TEXT_ROLES = ["instruct", "reasoning", "code", "json", "factual"]

# Fixed retrieval corpus + queries for the embeddings pack (answer = doc index).
_EMBED_CORPUS = [
    "The Eiffel Tower is a wrought-iron lattice tower in Paris, France.",
    "Photosynthesis lets plants convert sunlight, water and CO2 into glucose.",
    "Python is a high-level programming language known for readable syntax.",
    "The Pacific is the largest and deepest of Earth's oceans.",
    "Mount Everest is the highest mountain above sea level on Earth.",
    "A transformer is a neural network architecture based on self-attention.",
    "Water boils at 100 degrees Celsius at standard atmospheric pressure.",
    "The mitochondrion is the organelle that produces most of a cell's ATP.",
]
_EMBED_QUERIES = [
    {"q": "Where is the Eiffel Tower located?", "ans": 0},
    {"q": "How do plants make food from light?", "ans": 1},
    {"q": "Which ocean is the biggest?", "ans": 3},
    {"q": "What is the tallest mountain in the world?", "ans": 4},
    {"q": "What neural architecture uses attention?", "ans": 5},
    {"q": "At what temperature does water boil?", "ans": 6},
]


# ─────────────────────────────────────────────────────────────────────────────
# MATCHERS
# ─────────────────────────────────────────────────────────────────────────────
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
_FENCE_RE = re.compile(r"```(?:json|python|\w+)?\s*(.*?)```", re.S)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _strip_fence(s: str) -> str:
    m = _FENCE_RE.search(s or "")
    return (m.group(1) if m else (s or "")).strip()


def _last_number(s: str) -> Optional[float]:
    hits = _NUM_RE.findall((s or "").replace(",", ""))
    if not hits:
        return None
    try:
        return float(hits[-1])
    except Exception:
        return None


def _first_json(s: str):
    """Best-effort: parse the first JSON object/array in the (possibly fenced) text."""
    txt = _strip_fence(s)
    try:
        return json.loads(txt)
    except Exception:
        pass
    # find the first {...} or [...] span
    for opn, cls in (("{", "}"), ("[", "]")):
        i = txt.find(opn)
        j = txt.rfind(cls)
        if 0 <= i < j:
            try:
                return json.loads(txt[i:j + 1])
            except Exception:
                continue
    return None


def _check(output: str, match: dict) -> bool:
    t = match.get("type")
    out_n = _norm(output).lower()
    try:
        if t == "exact":
            return out_n == _norm(str(match["value"])).lower()
        if t == "contains":
            return str(match["value"]).lower() in out_n
        if t == "regex":
            return bool(re.search(match["pattern"], output or "", re.I))
        if t == "numeric":
            n = _last_number(output)
            if n is None:
                return False
            return abs(n - float(match["value"])) <= float(match.get("tol", 0.001))
        if t == "json_eq":
            return _first_json(output) == match["value"]
        if t == "json_has":
            doc = _first_json(output)
            if not isinstance(doc, dict):
                return False
            return all(doc.get(k) == v for k, v in match["value"].items())
        if t == "json_valid":
            return _first_json(output) is not None
    except Exception:
        return False
    return False


# ─────────────────────────────────────────────────────────────────────────────
# OLLAMA CALLS  (direct to the chosen node — never the router)
# ─────────────────────────────────────────────────────────────────────────────
def _metrics(resp: dict) -> dict:
    """Pull real timing metrics out of an Ollama /api/generate response."""
    def _s(ns):
        return (float(ns) / 1e9) if ns else 0.0
    pec = resp.get("prompt_eval_count") or 0
    ped = _s(resp.get("prompt_eval_duration"))
    ec = resp.get("eval_count") or 0
    ed = _s(resp.get("eval_duration"))
    load_s = _s(resp.get("load_duration"))
    return {
        "load_ms": round(load_s * 1000, 1),
        "prompt_tps": round(pec / ped, 1) if ped > 0 else None,
        "gen_tps": round(ec / ed, 1) if ed > 0 else None,
        "ttft_ms": round((load_s + ped) * 1000, 1),
        "total_s": round(_s(resp.get("total_duration")), 3),
        "gen_tokens": ec,
        "prompt_tokens": pec,
    }


async def _generate(url: str, model: str, prompt: str, num_predict: int = 128,
                    images: Optional[List[str]] = None, timeout: int = 120,
                    gopts: Optional[dict] = None) -> dict:
    g = gopts or {}
    body = {
        "model": model, "prompt": prompt, "stream": False,
        "options": {"temperature": float(g.get("temperature", 0.0)),
                    "seed": int(g.get("seed", 42)),
                    "top_p": float(g.get("top_p", 1.0)),
                    "num_predict": int(num_predict)},
    }
    if images:
        body["images"] = images
    async with httpx.AsyncClient(timeout=timeout, verify=_ssl()) as c:
        r = await c.post(f"{url}/api/generate", json=body)
        r.raise_for_status()
        return r.json() or {}


async def _unload(url: str, model: str) -> None:
    """Evict the model from VRAM so the next call measures a true cold load."""
    try:
        async with httpx.AsyncClient(timeout=30, verify=_ssl()) as c:
            await c.post(f"{url}/api/generate", json={"model": model, "keep_alive": 0})
    except Exception as e:
        log.debug("unload %s: %s", model, e)


async def _embed_one(url: str, model: str, text: str, timeout: int = 60) -> List[float]:
    async with httpx.AsyncClient(timeout=timeout, verify=_ssl()) as c:
        r = await c.post(f"{url}/api/embeddings", json={"model": model, "prompt": text})
        r.raise_for_status()
        return (r.json() or {}).get("embedding") or []


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else -1.0


# ─────────────────────────────────────────────────────────────────────────────
# SYNTHETIC IMAGE  (solid-colour PNG, base64 — for the vision pack, no PIL)
# ─────────────────────────────────────────────────────────────────────────────
def _solid_png_b64(r: int, g: int, b: int, size: int = 48) -> str:
    def _chunk(typ: bytes, data: bytes) -> bytes:
        c = typ + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
    raw = (bytes([0]) + bytes([r, g, b]) * size) * size          # filter byte + RGB row
    png = (b"\x89PNG\r\n\x1a\n"
           + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
           + _chunk(b"IDAT", zlib.compress(raw))
           + _chunk(b"IEND", b""))
    return base64.b64encode(png).decode()


# ─────────────────────────────────────────────────────────────────────────────
# PACK RUNNERS
# ─────────────────────────────────────────────────────────────────────────────
async def _run_generate_pack(url: str, model: str, role: str, pack: dict,
                             perf: dict, gopts: dict, repeats: int = 1) -> dict:
    # When repeats>1 (used at temperature>0 to average sampling variance), each
    # item is run `repeats` times and every run counts toward passed/total, so
    # accuracy becomes a pass-RATE rather than a single deterministic verdict.
    items_out, passed, total = [], 0, 0
    npred = int(pack.get("num_predict", 128))
    for it in pack.get("items", []):
        oks, last_out, last_tps, t0 = 0, "", None, time.time()
        for _ in range(max(1, repeats)):
            try:
                resp = await _generate(url, model, it["prompt"], num_predict=npred, gopts=gopts)
                out = resp.get("response", "") or ""
                m = _metrics(resp)
                _acc_perf(perf, m)
                oks += 1 if _check(out, it["match"]) else 0
                last_out, last_tps = out, m["gen_tps"]
            except Exception as e:
                last_out = f"[error: {str(e)[:120]}]"
            total += 1
        passed += oks
        items_out.append({
            "prompt": it["prompt"], "ok": oks == max(1, repeats),
            "passed": oks, "runs": max(1, repeats),
            "got": _norm(last_out)[:200], "gen_tps": last_tps,
            "wall_s": round(time.time() - t0, 2)})
    return {"role": role, "label": pack["label"], "kind": "generate",
            "passed": passed, "total": total,
            "accuracy": round(passed / total, 3) if total else None,
            "items": items_out}


async def _run_vision_pack(url: str, model: str, pack: dict, perf: dict, gopts: dict) -> dict:
    items_out, passed = [], 0
    for it in pack.get("items", []):
        img = _solid_png_b64(*it["color"])
        t0 = time.time()
        try:
            resp = await _generate(url, model, it["prompt"], num_predict=32,
                                   images=[img], gopts=gopts)
            out = resp.get("response", "") or ""
            m = _metrics(resp)
            ok = _check(out, it["match"])
            _acc_perf(perf, m)
        except Exception as e:
            items_out.append({"prompt": it["prompt"], "ok": False,
                              "got": f"[error: {str(e)[:120]}]", "wall_s": round(time.time() - t0, 2)})
            continue
        passed += 1 if ok else 0
        items_out.append({"prompt": it["prompt"], "ok": ok, "got": _norm(out)[:120],
                          "wall_s": round(time.time() - t0, 2)})
    total = len(pack.get("items", []))
    return {"role": "vision", "label": pack["label"], "kind": "vision",
            "passed": passed, "total": total,
            "accuracy": round(passed / total, 3) if total else None,
            "items": items_out}


async def _run_embed_pack(url: str, model: str, pack: dict) -> dict:
    try:
        doc_vecs = [await _embed_one(url, model, d) for d in _EMBED_CORPUS]
    except Exception as e:
        return {"role": "embed", "label": pack["label"], "kind": "embed",
                "passed": 0, "total": len(_EMBED_QUERIES), "accuracy": None,
                "error": f"embeddings unavailable: {str(e)[:140]}", "items": []}
    if not any(doc_vecs):
        return {"role": "embed", "label": pack["label"], "kind": "embed",
                "passed": 0, "total": len(_EMBED_QUERIES), "accuracy": None,
                "error": "model returned no embeddings (not an embedding model?)",
                "items": []}
    rr_sum, items_out, hits = 0.0, [], 0
    for q in _EMBED_QUERIES:
        try:
            qv = await _embed_one(url, model, q["q"])
        except Exception as e:
            items_out.append({"prompt": q["q"], "ok": False, "got": f"[error: {str(e)[:80]}]"})
            continue
        sims = sorted(((i, _cosine(qv, dv)) for i, dv in enumerate(doc_vecs)),
                      key=lambda x: -x[1])
        rank = next((r + 1 for r, (i, _) in enumerate(sims) if i == q["ans"]), 0)
        rr = 1.0 / rank if rank else 0.0
        rr_sum += rr
        top1 = sims[0][0] == q["ans"]
        hits += 1 if top1 else 0
        items_out.append({"prompt": q["q"], "ok": top1,
                          "got": f"top-1 doc #{sims[0][0]} (rank {rank}, RR {rr:.2f})"})
    total = len(_EMBED_QUERIES)
    # accuracy for embeddings = MRR (0..1), a fairer retrieval-quality score.
    return {"role": "embed", "label": pack["label"], "kind": "embed",
            "passed": hits, "total": total,
            "accuracy": round(rr_sum / total, 3) if total else None,
            "metric": "MRR", "items": items_out}


def _acc_perf(perf: dict, m: dict) -> None:
    if m.get("gen_tps"):
        perf["_gen_tps"].append(m["gen_tps"])
    if m.get("prompt_tps"):
        perf["_prompt_tps"].append(m["prompt_tps"])
    if m.get("ttft_ms"):
        perf["_ttft"].append(m["ttft_ms"])
    perf["_total_s"] += m.get("total_s") or 0.0
    perf["calls"] += 1


def _finalise_perf(perf: dict) -> dict:
    def _avg(xs):
        return round(sum(xs) / len(xs), 1) if xs else None
    return {
        "load_ms": perf.get("load_ms"),
        "cold": perf.get("cold", False),
        "gen_tps": _avg(perf["_gen_tps"]),
        "prompt_tps": _avg(perf["_prompt_tps"]),
        "ttft_ms": _avg(perf["_ttft"]),
        "total_wall_s": round(perf["_total_s"], 2),
        "calls": perf["calls"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# INSTALLED-MODEL METADATA  (size / params / quant, from the node's tag store)
# ─────────────────────────────────────────────────────────────────────────────
async def _model_meta(url: str, model: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=8, verify=_ssl()) as c:
            r = await c.get(f"{url}/api/tags")
            r.raise_for_status()
            for m in (r.json() or {}).get("models", []) or []:
                if (m.get("name") or m.get("model")) == model:
                    det = m.get("details") or {}
                    return {"size_gb": round((m.get("size") or 0) / 1e9, 2),
                            "quant": det.get("quantization_level", ""),
                            "params": det.get("parameter_size", ""),
                            "family": det.get("family", "")}
    except Exception:
        pass
    return {}


def _roles_for(role: str) -> List[str]:
    role = (role or "all").strip().lower()
    if role in ("all", "text", ""):
        return list(TEXT_ROLES)
    if role in ROLE_PACKS:
        return [role]
    return []


# ─────────────────────────────────────────────────────────────────────────────
# CORE BENCHMARK
# ─────────────────────────────────────────────────────────────────────────────
async def _benchmark(instance_id: str, model: str, role: str = "all",
                     cold: bool = False, run_id: str = "",
                     temperature: float = 0.0, top_p: float = 1.0,
                     seed: int = 42, repeats: int = 1) -> dict:
    url = _instance_url(instance_id)
    if not url:
        return {"error": f"unknown or URL-less Ollama node: {instance_id}"}
    if not (model or "").strip():
        return {"error": "model required"}
    roles = _roles_for(role)
    if not roles:
        return {"error": f"unknown role: {role}", "roles": list(ROLE_PACKS)}
    node = _instance(instance_id) or {}
    rid = run_id or f"bench-{uuid.uuid4().hex[:8]}"
    temperature = max(0.0, float(temperature))
    repeats = max(1, min(int(repeats or 1), 5))
    # At temperature 0 the run is deterministic, so extra repeats add cost for
    # no signal — collapse them. Above 0, keep repeats to average the variance.
    if temperature <= 0:
        repeats = 1
    gopts = {"temperature": temperature, "top_p": float(top_p), "seed": int(seed)}
    meta = await _model_meta(url, model)
    perf = {"_gen_tps": [], "_prompt_tps": [], "_ttft": [], "_total_s": 0.0,
            "calls": 0, "load_ms": None, "cold": bool(cold)}

    # Cold-load measurement: unload, then read the first call's load_duration.
    if cold:
        await _unload(url, model)
        await asyncio.sleep(0.5)
    try:
        warm = await _generate(url, model, "Reply with the single word: ok",
                               num_predict=8, gopts=gopts)
        perf["load_ms"] = _metrics(warm).get("load_ms")
    except Exception as e:
        return {"error": f"node unreachable or model not installed: {str(e)[:160]}",
                "instance_id": instance_id, "model": model}

    packs = []
    for r in roles:
        await _emit_progress(run_id, rid, model, instance_id, r, "start")
        pack = ROLE_PACKS[r]
        kind = pack.get("kind", "generate")
        if kind == "embed":
            res = await _run_embed_pack(url, model, pack)
        elif kind == "vision":
            res = await _run_vision_pack(url, model, pack, perf, gopts)
        else:
            res = await _run_generate_pack(url, model, r, pack, perf, gopts, repeats)
        packs.append(res)
        await _emit_progress(run_id, rid, model, instance_id, r, "done",
                             accuracy=res.get("accuracy"))

    scored = [p for p in packs if p.get("accuracy") is not None]
    total_items = sum(p["total"] for p in scored)
    passed_items = sum(p["passed"] for p in scored)
    accuracy = round(sum(p["accuracy"] for p in scored) / len(scored), 3) if scored else None
    perf_out = _finalise_perf(perf)

    record = {
        "id": rid, "created_at": now_iso(),
        "instance_id": instance_id, "node_label": node.get("label", instance_id),
        "has_gpu": bool(node.get("has_gpu")),
        "model": model, "role": role, "roles": roles,
        "cold": bool(cold),
        "sampling": {"temperature": temperature, "top_p": float(top_p),
                     "seed": int(seed), "repeats": repeats},
        "accuracy": accuracy,
        "items_total": total_items, "items_passed": passed_items,
        "perf": perf_out,
        "packs": packs,
        **meta,
        "ok": True,
    }
    await _store_result(record)
    await emit_event({"type": "bench.done", "id": rid, "model": model,
                      "instance_id": instance_id, "role": role,
                      "accuracy": accuracy, "gen_tps": perf_out.get("gen_tps"),
                      "load_ms": perf_out.get("load_ms")})
    return record


async def _emit_progress(run_id, rid, model, instance_id, role, stage, accuracy=None):
    if not run_id:
        return
    await emit_event({"type": "bench.progress", "run_id": run_id, "id": rid,
                      "model": model, "instance_id": instance_id, "role": role,
                      "stage": stage, "accuracy": accuracy})


# ─────────────────────────────────────────────────────────────────────────────
# RESULT STORE
# ─────────────────────────────────────────────────────────────────────────────
def _compact(record: dict) -> dict:
    # "params" = the model's parameter_size ("7.6B"); "sampling" = the run's
    # temperature/seed/top_p/repeats — kept as separate keys to avoid a clobber.
    return {k: record.get(k) for k in
            ("id", "created_at", "instance_id", "node_label", "model", "role",
             "roles", "cold", "accuracy", "items_total", "items_passed", "perf",
             "size_gb", "quant", "params", "sampling", "has_gpu")}


async def _store_result(record: dict) -> None:
    r = _redis()
    if not r:
        return
    try:
        await r.set(KEY_RESULT + record["id"], json.dumps(record), ex=RESULT_TTL)
        await r.lpush(KEY_RESULTS, json.dumps(_compact(record)))
        await r.ltrim(KEY_RESULTS, 0, RESULTS_CAP - 1)
    except Exception as e:
        log.warning("store bench result: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# CAPABILITIES
# ─────────────────────────────────────────────────────────────────────────────
@capability("bench.suites", memory="off", silent=True,
            http_method="GET", http_path="/bench/suites", http_tags=["bench"],
            description="List the deterministic benchmark ROLE PACKS: id, label, what it "
                        "measures, item count and kind (generate|embed|vision). These are "
                        "the accuracy suites bench.run scores a model against.")
async def cap_bench_suites(trace_id=None):
    out = []
    for rid, p in ROLE_PACKS.items():
        out.append({"role": rid, "label": p["label"], "blurb": p.get("blurb", ""),
                    "kind": p.get("kind", "generate"),
                    "items": len(p.get("items", [])) or len(_EMBED_QUERIES),
                    "text_default": rid in TEXT_ROLES})
    return {"suites": out, "text_roles": TEXT_ROLES,
            "note": "role='all' runs the text packs; embed/vision run only when selected."}


@capability("bench.run", memory="off",
            http_method="POST", http_path="/bench/run", http_tags=["bench"],
            description="Benchmark ONE model on ONE Ollama node, synchronously. Measures "
                        "real token throughput (gen/prompt tokens-sec), load time and "
                        "deterministic accuracy on the selected role pack(s). Inputs: "
                        "instance_id (str! — an Ollama node id from ollama.instances), "
                        "model (str! — an installed tag, e.g. 'qwen2.5:7b'), role "
                        "(all|instruct|reasoning|code|json|factual|embed|vision, default "
                        "all), cold (bool — unload first to measure a true cold load), "
                        "temperature (float=0 — 0 is deterministic/repeatable, >0 samples "
                        "so accuracy becomes a pass-rate), top_p (float=1.0), seed "
                        "(int=42), repeats (int=1, max 5 — samples per item at "
                        "temperature>0 to average variance). "
                        "Output: the full result record (accuracy, perf, per-item packs). "
                        "For big models prefer bench.run.start (background).",
            schema=enum_schema(role=["all", "instruct", "reasoning", "code", "json",
                                     "factual", "embed", "vision"]))
async def cap_bench_run(instance_id: str = "", model: str = "", role: str = "all",
                        cold: bool = False, temperature: float = 0.0, top_p: float = 1.0,
                        seed: int = 42, repeats: int = 1, trace_id=None):
    return await _benchmark(instance_id, model, role, cold, temperature=temperature,
                            top_p=top_p, seed=seed, repeats=repeats)


@capability("bench.run.start", memory="off",
            http_method="POST", http_path="/bench/run/start", http_tags=["bench"],
            description="Start a benchmark in the BACKGROUND and return a run_id "
                        "immediately; progress streams as bench.progress events and the "
                        "result lands in bench.results. Same inputs as bench.run "
                        "(instance_id!, model!, role, cold, temperature, top_p, seed, "
                        "repeats). Best for large/slow models.",
            schema=enum_schema(role=["all", "instruct", "reasoning", "code", "json",
                                     "factual", "embed", "vision"]))
async def cap_bench_run_start(instance_id: str = "", model: str = "", role: str = "all",
                              cold: bool = False, temperature: float = 0.0,
                              top_p: float = 1.0, seed: int = 42, repeats: int = 1,
                              trace_id=None):
    if not _instance_url(instance_id):
        return {"error": f"unknown Ollama node: {instance_id}"}
    if not (model or "").strip():
        return {"error": "model required"}
    rid = f"bench-{uuid.uuid4().hex[:8]}"
    status = {"run_id": rid, "state": "running", "model": model,
              "instance_id": instance_id, "role": role, "cold": bool(cold),
              "temperature": float(temperature), "started_at": now_iso()}
    r = _redis()
    if r:
        try:
            await r.hset(KEY_RUNS, rid, json.dumps(status))
            await r.expire(KEY_RUNS, 3600)
        except Exception:
            pass

    async def _bg():
        try:
            rec = await _benchmark(instance_id, model, role, cold, run_id=rid,
                                   temperature=temperature, top_p=top_p, seed=seed,
                                   repeats=repeats)
            st = {**status, "state": "error" if rec.get("error") else "done",
                  "finished_at": now_iso(), "result_id": rec.get("id"),
                  "accuracy": rec.get("accuracy"), "error": rec.get("error", "")}
        except Exception as e:
            st = {**status, "state": "error", "finished_at": now_iso(),
                  "error": str(e)[:200]}
        if r:
            try:
                await r.hset(KEY_RUNS, rid, json.dumps(st))
            except Exception:
                pass
        await emit_event({"type": "bench.run.finished", "run_id": rid,
                          "state": st["state"], "accuracy": st.get("accuracy")})

    asyncio.create_task(_bg())
    return {"ok": True, "run_id": rid, "state": "running"}


@capability("bench.status", memory="off", silent=True,
            http_method="GET", http_path="/bench/status", http_tags=["bench"],
            description="Live status of background benchmark runs (bench.run.start). "
                        "Query: run_id (str — one run, default all active).")
async def cap_bench_status(run_id: str = "", trace_id=None):
    r = _redis()
    rows = []
    if r:
        try:
            raw = await r.hgetall(KEY_RUNS)
            for k, v in (raw or {}).items():
                k = k.decode() if isinstance(k, bytes) else k
                v = v.decode() if isinstance(v, bytes) else v
                try:
                    rows.append(json.loads(v))
                except Exception:
                    continue
        except Exception:
            pass
    if run_id:
        rows = [x for x in rows if x.get("run_id") == run_id]
    rows.sort(key=lambda x: x.get("started_at") or "", reverse=True)
    active = [x for x in rows if x.get("state") == "running"]
    return {"runs": rows[:40], "active": len(active)}


@capability("bench.results", memory="off", silent=True,
            http_method="GET", http_path="/bench/results", http_tags=["bench"],
            description="Recent benchmark results (compact, newest first). Query: model "
                        "(str filter), instance_id (str filter), role (str filter), limit "
                        "(int, default 60).")
async def cap_bench_results(model: str = "", instance_id: str = "", role: str = "",
                            limit: int = 60, trace_id=None):
    r = _redis()
    out = []
    if r:
        try:
            rows = await r.lrange(KEY_RESULTS, 0, RESULTS_CAP - 1)
            for row in rows or []:
                try:
                    rec = json.loads(row.decode() if isinstance(row, bytes) else row)
                except Exception:
                    continue
                if model and rec.get("model") != model:
                    continue
                if instance_id and rec.get("instance_id") != instance_id:
                    continue
                if role and role != "all" and role not in (rec.get("roles") or []) \
                        and rec.get("role") != role:
                    continue
                out.append(rec)
                if len(out) >= int(limit):
                    break
        except Exception:
            pass
    return {"results": out, "count": len(out)}


@capability("bench.result.get", memory="off", silent=True,
            http_method="GET", http_path="/bench/result", http_tags=["bench"],
            description="Full detail of one benchmark result (per-pack, per-item outputs "
                        "and metrics). Input: id (str!).")
async def cap_bench_result_get(id: str = "", trace_id=None):
    r = _redis()
    if not r or not id:
        return {"error": "id required"}
    raw = await r.get(KEY_RESULT + id)
    if not raw:
        return {"error": "result not found (expired?)"}
    return {"result": json.loads(raw.decode() if isinstance(raw, bytes) else raw)}


@capability("bench.compare", memory="off", silent=True,
            http_method="GET", http_path="/bench/compare", http_tags=["bench"],
            description="Role LEADERBOARD: the latest benchmark result per (model, node) "
                        "for a role, ranked by accuracy then generation tokens-sec — the "
                        "'which model is best in this role' view. Query: role "
                        "(all|instruct|reasoning|code|json|factual|embed|vision, default "
                        "all), instance_id (str — restrict to one node). Output: "
                        "{role, rows:[{model,instance_id,node_label,accuracy,gen_tps,"
                        "load_ms,size_gb,quant,created_at}]}.",
            schema=enum_schema(role=["all", "instruct", "reasoning", "code", "json",
                                     "factual", "embed", "vision"]))
async def cap_bench_compare(role: str = "all", instance_id: str = "", trace_id=None):
    role = (role or "all").strip().lower()
    res = await cap_bench_results(instance_id=instance_id, limit=RESULTS_CAP)
    latest: Dict[str, dict] = {}
    for rec in res.get("results", []):
        if instance_id and rec.get("instance_id") != instance_id:
            continue
        # A leaderboard row must be scored FOR this exact role, so the accuracy
        # shown is that role's score — never an 'all'-bundle mean stood in for a
        # single-role board (or vice-versa).
        if rec.get("role", "all").lower() != role:
            continue
        key = f"{rec.get('model')}@{rec.get('instance_id')}"
        # results are newest-first — keep the first (latest) per model/node
        if key not in latest:
            latest[key] = rec
    rows = []
    for rec in latest.values():
        perf = rec.get("perf") or {}
        rows.append({
            "model": rec.get("model"), "instance_id": rec.get("instance_id"),
            "node_label": rec.get("node_label"), "has_gpu": rec.get("has_gpu"),
            "accuracy": rec.get("accuracy"),
            "items_passed": rec.get("items_passed"), "items_total": rec.get("items_total"),
            "gen_tps": perf.get("gen_tps"), "prompt_tps": perf.get("prompt_tps"),
            "load_ms": perf.get("load_ms"), "ttft_ms": perf.get("ttft_ms"),
            "size_gb": rec.get("size_gb"), "quant": rec.get("quant"),
            "params": rec.get("params"), "cold": rec.get("cold"),
            "sampling": rec.get("sampling"),
            # live_tps: observed throughput from real traffic (passive), if any —
            # lets the board show benchmarked vs. in-production speed side by side.
            "live_tps": _observed_tps(rec.get("model"), rec.get("instance_id")),
            "result_id": rec.get("id"), "created_at": rec.get("created_at")})
    rows.sort(key=lambda x: (-(x["accuracy"] if x["accuracy"] is not None else -1),
                             -(x["gen_tps"] or 0)))
    return {"role": role, "rows": rows, "count": len(rows)}


@capability("bench.passive", memory="off", silent=True,
            http_method="GET", http_path="/bench/passive", http_tags=["bench"],
            description="PASSIVE benchmark: per-(model, node) metrics harvested from REAL "
                        "production traffic (the router's rolling per-request stats every "
                        "ollama_generate call feeds) — no test is run, so there is no "
                        "accuracy, only observed tokens-sec, average latency, request "
                        "count and the job types seen. The 'how is this model actually "
                        "performing in use' view, complementing the active leaderboard. "
                        "Query: instance_id (str — restrict to one node). Output: "
                        "{rows:[{model,instance_id,node_label,obs_tps,avg_latency_s,"
                        "calls,job_types,avg_prompt_chars,last_ts}], total_calls}.")
async def cap_bench_passive(instance_id: str = "", trace_id=None):
    rows = _passive_rows(instance_id)
    return {"rows": rows, "count": len(rows),
            "total_calls": sum(r.get("calls") or 0 for r in rows)}


@capability("bench.clear", memory="off",
            http_method="POST", http_path="/bench/clear", http_tags=["bench"],
            description="Delete stored benchmark results. Inputs: id (str — one result; "
                        "empty clears ALL results).")
async def cap_bench_clear(id: str = "", trace_id=None):
    r = _redis()
    if not r:
        return {"error": "no redis"}
    if id:
        try:
            await r.delete(KEY_RESULT + id)
            rows = await r.lrange(KEY_RESULTS, 0, RESULTS_CAP - 1)
            keep = [row for row in (rows or [])
                    if _safe_id(row) != id]
            await r.delete(KEY_RESULTS)
            if keep:
                await r.rpush(KEY_RESULTS, *keep)
        except Exception as e:
            return {"error": str(e)}
        return {"ok": True, "cleared": id}
    try:
        rows = await r.lrange(KEY_RESULTS, 0, RESULTS_CAP - 1)
        for row in rows or []:
            rid = _safe_id(row)
            if rid:
                await r.delete(KEY_RESULT + rid)
        await r.delete(KEY_RESULTS)
    except Exception as e:
        return {"error": str(e)}
    return {"ok": True, "cleared": "all"}


def _safe_id(row) -> str:
    try:
        return json.loads(row.decode() if isinstance(row, bytes) else row).get("id", "")
    except Exception:
        return ""


@capability("bench.loop", memory="off",
            http_method="POST", http_path="/bench/loop", http_tags=["bench"],
            description="LOOP-LAB mechanism: benchmark a model qualitatively by pinning it "
                        "onto its node and running a real agentic loop profile against a "
                        "goal (loops.run with model+instance override). Complements the "
                        "deterministic packs with an agentic read. Inputs: instance_id "
                        "(str!), model (str!), goal (str!), profile (str='planning'), "
                        "max_steps (int=6). Output: the loop run result.")
async def cap_bench_loop(instance_id: str = "", model: str = "", goal: str = "",
                         profile: str = "planning", max_steps: int = 6, trace_id=None):
    if not (_instance_url(instance_id) and model and (goal or "").strip()):
        return {"error": "instance_id, model and goal are all required"}
    run = _rawcap("loops.run")
    if not run:
        return {"error": "loops.run unavailable (dag/loop_profiles not loaded)"}
    res = await run(profile=profile, goal=goal, model=model,
                    instance_id=instance_id, max_steps=int(max_steps or 6))
    await emit_event({"type": "bench.loop.done", "model": model,
                      "instance_id": instance_id, "profile": profile})
    return {"ok": not (isinstance(res, dict) and res.get("error")),
            "instance_id": instance_id, "model": model, "profile": profile,
            "goal": goal, "result": res}


# ═════════════════════════════════════════════════════════════════════════════
# PER-NODE PERFORMANCE MONITOR
# ─────────────────────────────────────────────────────────────────────────────
# Every Ollama node has a different setup (GPU/CPU, VRAM, disk) and carries a
# different live workload. This section gives a node-centric view: live
# reachability + ping, resident models and the VRAM they hold (/api/ps),
# hardware + free disk (from the catalog), and the workload each node is actually
# serving (aggregated from the router's rolling stats). A lightweight sampler
# keeps a short per-node time-series for sparklines; GPU utilisation is an
# on-demand SSH probe (nvidia-smi) so nothing polls SSH in the background.
# ═════════════════════════════════════════════════════════════════════════════
def _mod(name: str):
    m = sys.modules.get(name)
    if m is not None:
        return m
    for k, v in list(sys.modules.items()):
        if v is not None and k.endswith(name):
            return v
    return None


async def _ping_node(url: str) -> dict:
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=4, verify=_ssl()) as c:
            r = await c.get(f"{url}/api/version")
            ok = r.status_code == 200
            return {"reachable": ok, "ping_ms": round((time.time() - t0) * 1000, 1),
                    "version": ((r.json() or {}).get("version", "") if ok else "")}
    except Exception:
        return {"reachable": False, "ping_ms": None, "version": ""}


async def _ps_node(url: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=5, verify=_ssl()) as c:
            r = await c.get(f"{url}/api/ps")
            if r.status_code != 200:
                return {"loaded": [], "loaded_vram_gb": 0.0}
            models = (r.json() or {}).get("models", []) or []
    except Exception:
        return {"loaded": [], "loaded_vram_gb": 0.0}
    loaded, tot = [], 0.0
    for m in models:
        v = round((m.get("size_vram") or 0) / 1e9, 2)
        tot += v
        loaded.append({"model": m.get("name", ""), "vram_gb": v,
                       "size_gb": round((m.get("size") or 0) / 1e9, 2),
                       "expires_at": m.get("expires_at", "")})
    return {"loaded": loaded, "loaded_vram_gb": round(tot, 2)}


def _node_workload(iid: str) -> dict:
    """What this node is actually serving, from the router's rolling stats."""
    calls, tps, el, jobs, last = 0, [], [], set(), ""
    models: Dict[str, dict] = {}
    for s in _route_stats().values():
        if s.get("instance") != iid:
            continue
        m = s.get("model", "?")
        n = int(s.get("n") or 0)
        calls += n
        if s.get("ema_tps"):
            tps.append(float(s["ema_tps"]))
        if s.get("ema_elapsed_s"):
            el.append(float(s["ema_elapsed_s"]))
        if s.get("job_type"):
            jobs.add(s["job_type"])
        mm = models.setdefault(m, {"model": m, "calls": 0, "tps": None})
        mm["calls"] += n
        if s.get("ema_tps"):
            mm["tps"] = max(mm["tps"] or 0.0, float(s["ema_tps"]))
        if s.get("last_ts", "") > last:
            last = s["last_ts"]
    return {"calls": calls,
            "tps": round(max(tps), 1) if tps else None,
            "avg_latency_s": round(sum(el) / len(el), 2) if el else None,
            "active_models": len(models),
            "job_types": sorted(jobs),
            "top_models": sorted(models.values(), key=lambda x: -x["calls"])[:5],
            "last_ts": last}


async def _build_node_perf() -> List[dict]:
    insts = getattr(_orch, "OLLAMA_INSTANCES", {}) or {}
    hw: Dict[str, dict] = {}
    fn = _rawcap("catalog.nodes")
    if fn:
        try:
            for n in (await fn() or {}).get("nodes", []) or []:
                hw[n.get("id")] = n
        except Exception as e:
            log.debug("node_perf catalog.nodes: %s", e)

    async def _one(iid: str, inst: dict) -> dict:
        url = inst.get("url", "")
        ping, ps = await asyncio.gather(_ping_node(url), _ps_node(url))
        h = hw.get(iid, {})
        vram = h.get("vram_gb")
        disk_free = h.get("models_disk_free_gb")
        if disk_free is None:
            disk_free = h.get("disk_free_gb")
        disk_total = h.get("models_disk_total_gb")
        if disk_total is None:
            disk_total = h.get("disk_total_gb")
        return {
            "id": iid, "label": inst.get("label", iid), "url": url,
            "enabled": inst.get("enabled", True), "has_gpu": inst.get("has_gpu", False),
            "priority": inst.get("priority", 0), "status": inst.get("status", ""),
            "hw": {"vram_gb": vram, "ram_gb": h.get("ram_gb"),
                   "gpu_name": h.get("gpu_name", ""), "cpu_cores": h.get("cpu_cores"),
                   "disk_free_gb": disk_free, "disk_total_gb": disk_total},
            "reachable": ping["reachable"], "ping_ms": ping["ping_ms"],
            "version": ping["version"],
            "loaded": ps["loaded"], "loaded_vram_gb": ps["loaded_vram_gb"],
            "vram_used_pct": (round(ps["loaded_vram_gb"] / vram * 100)
                              if vram else None),
            "workload": _node_workload(iid),
        }

    res = await asyncio.gather(*[_one(iid, inst) for iid, inst in insts.items()],
                               return_exceptions=True)
    rows = [r for r in res if isinstance(r, dict)]
    rows.sort(key=lambda n: (not n["reachable"], -(n["hw"].get("vram_gb") or 0),
                             str(n["label"]).lower()))
    return rows


# Sampler cache + short per-node history ring (in-memory, like sysmon).
NODE_PERF_SAMPLE_SEC = 15.0
NODE_PERF_HIST_MAX = 240                         # ~1h at 15s
_NODE_PERF = {"at": 0.0, "rows": []}
_NODE_HIST: Dict[str, Deque[dict]] = {}


def _hist_push(rows: List[dict]) -> None:
    now = time.time()
    for n in rows:
        dq = _NODE_HIST.setdefault(n["id"], deque(maxlen=NODE_PERF_HIST_MAX))
        wl = n.get("workload") or {}
        dq.append({"t": now, "tps": wl.get("tps"), "calls": wl.get("calls"),
                   "loaded_vram_gb": n.get("loaded_vram_gb"),
                   "ping_ms": n.get("ping_ms"),
                   "reachable": 1 if n.get("reachable") else 0,
                   "loaded": len(n.get("loaded") or [])})


async def _node_perf_tick() -> None:
    try:
        rows = await _build_node_perf()
        _NODE_PERF.update(at=time.time(), rows=rows)
        _hist_push(rows)
    except Exception as e:
        log.debug("node_perf tick: %s", e)


@capability("bench.node_perf", memory="off", silent=True,
            http_method="GET", http_path="/bench/node_perf", http_tags=["bench"],
            description="PER-NODE live performance monitor for the Ollama cluster — each "
                        "node with its own setup + workload. Per node: reachability + ping "
                        "ms, resident models and the VRAM they hold (/api/ps), hardware "
                        "(VRAM/RAM/GPU/cores) + free model-store disk, and the live "
                        "workload it is serving (calls, observed tokens-sec, avg latency, "
                        "active models, job-type mix, top models) from the router's "
                        "rolling stats. Served from a ~15s sampler cache; pass fresh=true "
                        "to rebuild now. Output: {ts, cached, age_s, nodes:[…]}.")
async def cap_bench_node_perf(fresh: bool = False, trace_id=None):
    age = (time.time() - _NODE_PERF["at"]) if _NODE_PERF["at"] else 1e9
    if fresh or not _NODE_PERF["rows"] or age > NODE_PERF_SAMPLE_SEC * 2.5:
        rows = await _build_node_perf()
        _NODE_PERF.update(at=time.time(), rows=rows)
        _hist_push(rows)
        return {"ts": now_iso(), "cached": False, "age_s": 0.0, "nodes": rows}
    return {"ts": now_iso(), "cached": True, "age_s": round(age, 1),
            "nodes": _NODE_PERF["rows"]}


@capability("bench.node_perf.history", memory="off", silent=True,
            http_method="GET", http_path="/bench/node_perf/history", http_tags=["bench"],
            description="Per-node time-series ring behind the monitor sparklines: samples "
                        "of {t, tps, calls, loaded_vram_gb, ping_ms, reachable, loaded}. "
                        "Query: instance_id (str — one node, default all), limit (int, "
                        "default 120). Output: {series:{instance_id:[…]}, interval_s}.")
async def cap_bench_node_perf_history(instance_id: str = "", limit: int = 120,
                                      trace_id=None):
    out: Dict[str, list] = {}
    for iid, dq in _NODE_HIST.items():
        if instance_id and iid != instance_id:
            continue
        items = list(dq)
        out[iid] = items[-int(limit):] if limit else items
    return {"series": out, "interval_s": NODE_PERF_SAMPLE_SEC,
            "count": sum(len(v) for v in out.values())}


async def _ssh_host_for(iid: str) -> str:
    cat = _mod("catalog_capabilities")
    if cat is not None:
        mapped = (getattr(cat, "NODE_SSH", {}) or {}).get(iid)
        if mapped:
            return mapped
    fn = _rawcap("nodes.list")
    if fn:
        try:
            for n in (await fn() or {}).get("nodes", []) or []:
                for o in n.get("ollama", []) or []:
                    if o.get("id") == iid:
                        return n.get("ssh_host_id", "") or ""
        except Exception:
            pass
    return ""


_GPU_QUERY = ("nvidia-smi --query-gpu=index,name,utilization.gpu,utilization.memory,"
              "memory.used,memory.total,temperature.gpu,power.draw "
              "--format=csv,noheader,nounits")


@capability("bench.node_gpu", memory="off",
            http_method="POST", http_path="/bench/node_gpu", http_tags=["bench"],
            description="Sample LIVE GPU utilisation for one Ollama node over SSH "
                        "(nvidia-smi): per-GPU util %, memory used/total, VRAM-util %, "
                        "temperature and power draw. On-demand only — nothing polls SSH "
                        "in the background. Inputs: instance_id (str! — an Ollama node). "
                        "The node must be mapped to an SSH host (catalog.node.ssh_set / "
                        "nodes). Output: {ok, instance_id, host_id, gpus:[…]}.")
async def cap_bench_node_gpu(instance_id: str = "", trace_id=None):
    if not instance_id:
        return {"error": "instance_id required"}
    host = await _ssh_host_for(instance_id)
    if not host:
        return {"error": "no SSH host mapped for this node — map one in the "
                         "Catalog › Nodes & Hardware tab first"}
    run = _rawcap("exec.ssh.run")
    if not run:
        return {"error": "exec.ssh.run unavailable"}
    r = await run(command=_GPU_QUERY, host_id=host, timeout=20) or {}
    txt = (r.get("stdout", "") if isinstance(r, dict) else "") or ""
    if not txt.strip():
        return {"error": r.get("error") or r.get("stderr", "")[:200]
                         or "no output (nvidia-smi missing or no GPU)",
                "instance_id": instance_id, "host_id": host}
    gpus = []
    for ln in txt.splitlines():
        p = [x.strip() for x in ln.split(",")]
        if len(p) < 6:
            continue
        def _f(v):
            try:
                return float(re.sub(r"[^\d.]", "", v) or 0)
            except Exception:
                return None
        used, total = _f(p[4]), _f(p[5])
        gpus.append({
            "index": p[0], "name": p[1],
            "util_pct": _f(p[2]), "mem_util_pct": _f(p[3]),
            "mem_used_mb": used, "mem_total_mb": total,
            "mem_pct": round(used / total * 100) if used and total else None,
            "temp_c": _f(p[6]) if len(p) > 6 else None,
            "power_w": _f(p[7]) if len(p) > 7 else None})
    return {"ok": True, "instance_id": instance_id, "host_id": host, "gpus": gpus}


# Opt-out-able background sampler for the per-node monitor.
try:
    schedule(_node_perf_tick, NODE_PERF_SAMPLE_SEC, name="bench_node_perf")
except Exception as e:
    log.debug("schedule node_perf: %s", e)


log.info("bench: model benchmarking capabilities loaded")
