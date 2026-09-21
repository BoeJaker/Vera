"""
nlp_dispatch_core.py  —  where NLP work runs, decided without importing the app
==============================================================================

Pure placement and routing decisions for `nlp.*`. No I/O, no orchestrator, no
Redis — so `tests/` can import it as `vera.research.nlp_dispatch_core` with the
worktree on `sys.path` and exercise the real logic (see the namespace-package
import trap in the dev-lifecycle notes: `Vera.vera.X` resolves to the MAIN
checkout, not your worktree).

Three decisions live here:

  1. resolve_placement()  — may this call run on the Vera host at all?
  2. pick_nlp_node()      — which node should serve it?
  3. chunk_text() / merge_chunk_entities() — how a document longer than the
     model's window is split and stitched back into ONE offset space.

WHY THIS IS SEPARATE FROM THE TASK QUEUE
────────────────────────────────────────
The obvious way to move `nlp.*` off the host is `dispatch_task()` onto
`vera:tasks`. Measured 2026-09-20, that moves nothing: every worker registered
with a non-empty capability set reports the SAME pid as the orchestrator
(`obs.workers` -> two workers, both pid 1124104 on host LLM, cap_count 2520;
the remaining registrations carry cap_count 0). The queue is distributed in
SHAPE and single-homed in FACT, so a task put on it is picked up by a consumer
inside the same process on the same 2-core VM.

So placement here is a transport decision — call a node's NLP server over HTTP,
the way cluster.py already routes inference — not a queue decision. If a real
off-host worker is ever registered, routing through the queue becomes viable
again and this module is where that choice would be re-made.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ── The model set ────────────────────────────────────────────────────────────
# One registry, read by BOTH the exporter (edge/nlp_export_models.py, which
# builds the ONNX artifacts) and the server (edge/nlp_server.py, which loads
# them). They must agree on the directory name for a model or the server will
# silently fall back to downloading from the hub at request time — on a node
# whose model store is mounted READ-ONLY, that fails at the worst moment.
#
# Chosen to be genuinely useful on CPU: base-sized, quantisation-friendly, and
# covering tasks an LLM call would otherwise be spent on. The ollama nodes share
# one read-only ZFS model store, so these are downloaded once and served by
# every node.
DEFAULT_MODELS: Dict[str, str] = {
    # NER. OntoNotes v5 — 18 types INCLUDING DATE/TIME, which CoNLL-2003 lacks
    # entirely and which is the reason NER was wanted here (timelines).
    "ner":        "djagatiya/ner-roberta-base-ontonotesv5-englishv4",
    # NER for text that is not English. PER/ORG/LOC across ten languages.
    "ner_multi":  "Davlan/xlm-roberta-base-ner-hrl",
    # Binary sentiment. The long-standing default; kept so nothing regresses.
    "classify":   "distilbert-base-uncased-finetuned-sst-2-english",
    # Three-class sentiment (neg/neu/pos) trained on short informal text —
    # far better than SST-2 on anything conversational.
    "sentiment3": "cardiffnlp/twitter-roberta-base-sentiment-latest",
    # Zero-shot classification: arbitrary caller-supplied labels, no training.
    # The highest-leverage model here — it replaces a whole class of LLM calls.
    "zeroshot":   "MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli",  # pragma: allowlist secret
    # Extractive QA: answer a question FROM a passage, with a span and a score.
    "qa":         "deepset/roberta-base-squad2",
    # Language identification, 20 languages — cheap routing/preprocessing.
    "langid":     "papluca/xlm-roberta-base-language-detection",
    # Sentence embeddings on CPU. Note the standing rule that embedding work is
    # never GPU-routed; this is a CPU model on a CPU path.
    "embed":      "sentence-transformers/all-MiniLM-L6-v2",  # pragma: allowlist secret
    # Cross-encoder reranker, loaded through fastembed rather than optimum.
    "rerank":     "Xenova/ms-marco-MiniLM-L-6-v2",
    # Zero-shot NER: the fabric's own entity engine (any label the caller names;
    # GLINER_LABELS_DEFAULT is the fabric's set), so the 2-core host stops
    # running the transformer itself. A torch checkpoint saved into the store
    # with save_pretrained — not an optimum export.
    "gliner":     "urchade/gliner_medium-v2.1",
    # Statistical NER with a full pipeline (sentences, POS, dependency parse)
    # the fabric's spaCy path used on the host. A pip package, pinned in the
    # component's install steps rather than a store directory.
    "spacy":      "en_core_web_sm",
}

#: The fabric's zero-shot label set (fabric_web_acquisition._GLINER_LABELS_DEFAULT
#: is the same list): what `gliner` looks for when the caller names nothing.
GLINER_LABELS_DEFAULT: List[str] = [
    "person", "organization", "company", "government agency", "location",
    "city", "country", "building", "landmark", "geographic feature",
    "product", "technology", "software", "programming language", "device",
    "vehicle", "creative work", "book", "film", "game", "song", "character",
    "event", "date", "money", "law", "field of study", "scientific concept",
    "biological species", "chemical", "medical condition", "job title",
    "nationality", "language", "award", "currency", "unit",
]

#: How each task's model is loaded. The exporter picks an ORT class from this
#: and the server picks a pipeline from it, so a task added in one place cannot
#: be forgotten in the other.
TASK_KIND: Dict[str, str] = {
    "ner":        "token-classification",
    "ner_multi":  "token-classification",
    "classify":   "text-classification",
    "sentiment3": "text-classification",
    "zeroshot":   "zero-shot-classification",
    "qa":         "question-answering",
    "langid":     "text-classification",
    "embed":      "feature-extraction",
    "rerank":     "fastembed",          # not an optimum export
    "gliner":     "gliner",             # a torch checkpoint the gliner package loads
    "spacy":      "spacy",              # a pip-installed pipeline package
}


def model_slug(model_id: str) -> str:
    """Directory name for a model inside the shared store.

    `org/name` -> `org__name`. Flat and reversible, so the store can be listed
    and matched against the registry by eye.
    """
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(model_id).replace("/", "__"))

# ── Placement ────────────────────────────────────────────────────────────────

#: Returned by resolve_placement().where
REMOTE = "remote"
LOCAL = "local"
FAIL = "fail"


def resolve_placement(nlp_local: bool, servers: Sequence[Any]) -> Dict[str, str]:
    """Decide where one `nlp.*` call may execute.

    `nlp_local` is the operator-facing switch (default OFF). OFF means the Vera
    host is NEVER allowed to run NLP — not "prefer a node, fall back to here".

    The fallback is the whole point of the switch, so it must not exist: a
    silent in-process fallback would burn the 2-core host exactly as before
    while making the switch look like it had worked. With no server available
    and the switch off, the caller gets FAIL and a reason naming the switch.
    """
    if servers:
        return {"where": REMOTE,
                "reason": f"{len(servers)} node(s) serving NLP"}
    if nlp_local:
        return {"where": LOCAL,
                "reason": "no node serving NLP; nlp.local is on, so the host may run it"}
    return {"where": FAIL,
            "reason": ("no node is serving NLP and nlp.local is off, so this host "
                       "must not run it — deploy the NLP server to a node, or set "
                       "nlp.local=true to allow in-process execution")}


# ── Node choice ──────────────────────────────────────────────────────────────

def score_nlp_node(node: Dict[str, Any]) -> float:
    """Penalty score for running NLP on `node` — LOWER is better.

    ⚠ `loadavg` is deliberately NOT a term in this score, and must not become
    one. The three "nodes" (gpu-250, cpu-246, cpu-247) are LXC containers on a
    single Proxmox host and share one kernel, so all three report the SAME
    load figure at any instant — measured twice minutes apart on 2026-09-20:
    [8.89, 13.08, 13.77] for all three, then 12.21 for all three, then
    [18.82, 15.92, 15.69] for all three. It is the HOST's number, not the
    container's. A router that reaches for it (everyone does, it is the obvious
    signal) will appear to work and will in fact be choosing at random.

    The signals that DO discriminate between these containers:
      • runners           — compute processes actually on that node
      • mem_available_mb  — real per-container memory headroom
      • gpu.util_pct      — meaningful on gpu-250 only
    """
    score = 0.0

    # Each compute process already on the node is a core we are competing for.
    score += float(node.get("runners") or 0) * 1.0

    # Memory pressure, as a fraction of the container's own total.
    total = float(node.get("mem_total_mb") or 0)
    avail = float(node.get("mem_available_mb") or 0)
    if total > 0:
        score += (1.0 - max(0.0, min(1.0, avail / total))) * 2.0

    # A node whose GPU is BUSY is a better CPU host, not a worse one: its
    # ollama runner is decoding on the V100 and is not spending the container's
    # twelve cores. That combination — GPU busy, CPU free — is the one place
    # NLP is genuinely cheap, so it earns a bonus rather than a penalty.
    gpu = node.get("gpu") or {}
    if gpu and float(gpu.get("util_pct") or 0) >= 50.0:
        score -= 0.5

    return score


def pick_nlp_node(candidates: Sequence[Dict[str, Any]]
                  ) -> Tuple[Optional[Dict[str, Any]], str]:
    """Choose among nodes ALREADY KNOWN to serve NLP. Returns (node, why).

    Callers filter for a reachable NLP server first — this function ranks, it
    does not probe. Ties break on node id so the choice is deterministic and a
    test can assert it.
    """
    usable = [n for n in candidates if n]
    if not usable:
        return None, "no candidate nodes"

    ranked = sorted(usable, key=lambda n: (score_nlp_node(n),
                                           str(n.get("node_id") or "")))
    best = ranked[0]
    why = (f"{best.get('node_id')}: score={score_nlp_node(best):.2f} "
           f"runners={best.get('runners')} "
           f"mem_free={best.get('mem_available_mb')}MB")
    if len(ranked) > 1:
        runner_up = ranked[1]
        why += (f" (next: {runner_up.get('node_id')} "
                f"score={score_nlp_node(runner_up):.2f})")
    return best, why


# ── Chunking ─────────────────────────────────────────────────────────────────

def chunk_text(text: str, max_chars: int = 1000,
               overlap: int = 0) -> List[Tuple[int, str]]:
    """Split `text` into (offset, chunk) pairs, offsets into the ORIGINAL text.

    `nlp.ner` truncated at `text[:1024]` — one paragraph of a document, silently
    dropping the rest and reporting success. Chunking replaces that, and the
    offsets are what let merge_chunk_entities() put the pieces back into one
    coordinate space the caller can index into.

    Chunks prefer to break on whitespace so an entity is less likely to be cut
    in half; `overlap` re-reads the tail of the previous chunk so an entity on a
    boundary still appears whole in one of them.
    """
    if not text:
        return []
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if overlap < 0 or overlap >= max_chars:
        raise ValueError("overlap must be >= 0 and < max_chars")

    out: List[Tuple[int, str]] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + max_chars, n)
        if end < n:
            # Back off to the last whitespace so we cut between words, but do
            # not give back so much that we make no progress.
            brk = text.rfind(" ", start, end)
            if brk > start + (max_chars // 2):
                end = brk
        out.append((start, text[start:end]))
        if end >= n:
            break
        # Always advance. A large overlap must slow the walk down, never stall
        # it — `end - overlap` can land at or behind `start` once the
        # whitespace back-off has already pulled `end` in.
        nxt = end - overlap
        start = nxt if nxt > start else end
    return out


def merge_chunk_entities(pieces: Iterable[Tuple[int, Sequence[Dict[str, Any]]]]
                         ) -> List[Dict[str, Any]]:
    """Re-base per-chunk entity offsets onto the original text and de-duplicate.

    `pieces` is (chunk_offset, entities) as returned per chunk. An entity's
    start/end are chunk-relative; adding the offset puts every entity in the
    document's coordinate space.

    Overlapping chunks legitimately report the same entity twice. Two entities
    are the same when they share (start, end, entity) after re-basing; the
    higher-scoring copy wins, so an entity seen whole in one chunk beats a
    truncated sighting of it in another.
    """
    best: Dict[Tuple[Any, Any, Any], Dict[str, Any]] = {}
    order: List[Tuple[Any, Any, Any]] = []

    for offset, entities in pieces:
        for ent in entities or []:
            e = dict(ent)
            if e.get("start") is not None:
                e["start"] = int(e["start"]) + int(offset)
            if e.get("end") is not None:
                e["end"] = int(e["end"]) + int(offset)
            key = (e.get("start"), e.get("end"), e.get("entity"))
            prev = best.get(key)
            if prev is None:
                best[key] = e
                order.append(key)
            elif float(e.get("score") or 0.0) > float(prev.get("score") or 0.0):
                best[key] = e

    merged = [best[k] for k in order]
    merged.sort(key=lambda e: (e.get("start") is None, e.get("start") or 0))
    return merged
