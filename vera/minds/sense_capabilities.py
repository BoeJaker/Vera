"""sense.frame: one Frame per text (four-minds FM-1.1; see sense_core).

Patterns always; the decision-space vector and domain cards when an NLP node answers inside its
timeout; NER only when asked (it is too slow for the chat PRE path); langid only for non-ASCII
text. NLP capabilities are called raw, so Sense writes no activity records of its own, and one call
in SAMPLE_EVERY emits a `sense.frame.timing` event. Frames are cached by text hash for an hour.
When the nodes are down the frame is patterns-only with `degraded: true`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY, capability, emit_event

try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import sense_core as SC
except Exception:                        # worktree / app-free import
    from vera.minds import sense_core as SC   # type: ignore

log = logging.getLogger("vera.minds.sense")

FRAME_TTL_S = 3600
EMBED_TIMEOUT_S = 1.5
NER_TIMEOUT_S = 4.0
LANGID_TIMEOUT_S = 1.0
SAMPLE_EVERY = 50
KEY_FRAME = "vera:sense:frame:"
KEY_VEC = "vera:sense:vec:"
KEY_CARDS = "vera:sense:cards:"
_CARDS: Dict[str, Dict[str, List[float]]] = {}      # card_hash -> {domain: vector}
_STATS: Dict[str, int] = {"calls": 0, "cached": 0, "degraded": 0}
_CARD_LOCK = asyncio.Lock()


async def _raw(name: str, /, timeout: float, **kw) -> Dict[str, Any]:
    cap = CAPABILITY_REGISTRY.get(name) or {}
    fn = cap.get("raw") or cap.get("func")
    if not fn:
        return {"error": f"{name} not available"}
    try:
        out = await asyncio.wait_for(fn(**kw), timeout)
        return out if isinstance(out, dict) else {"error": f"{name}: unexpected reply"}
    except Exception as e:
        return {"error": f"{name}: {type(e).__name__}"}


def _redis():
    return getattr(_orch, "REDIS", None)


async def _cards() -> Dict[str, List[float]]:
    """The domain cards' vectors, embedded once per card text (in process, then Redis)."""
    h = SC.card_hash(SC.DOMAIN_CARDS)
    if h in _CARDS:
        return _CARDS[h]
    async with _CARD_LOCK:
        if h in _CARDS:
            return _CARDS[h]
        r = _redis()
        if r is not None:
            try:
                raw = await r.get(KEY_CARDS + h)
                got = json.loads(raw) if raw else None
                # v2 payload: the vectors with the model and dimension that made them (backbone review 11 Oct);
                # a v1 payload (bare vectors, no identity) is re-embedded once
                if isinstance(got, dict) and isinstance(got.get("vectors"), dict):
                    _CARDS[h] = got["vectors"]
                    return _CARDS[h]
            except Exception:
                pass
        names = list(SC.DOMAIN_CARDS)
        res = await _raw("nlp.embed", 10.0, texts=[SC.DOMAIN_CARDS[n] for n in names])
        vecs = res.get("embeddings") if isinstance(res.get("embeddings"), list) else []
        if len(vecs) != len(names):
            return {}
        cards = {n: v for n, v in zip(names, vecs)}
        _CARDS[h] = cards
        if r is not None:
            try:
                await r.set(KEY_CARDS + h, json.dumps({"model": res.get("model") or "", "dim": len(vecs[0] or []),
                                                       "vectors": cards}))
            except Exception:
                pass
        return cards


try:                                     # pragma: no cover - import-shape only
    from Vera.vera.model_binding_core import binding as _bind
except Exception:                        # worktree / app-free import
    from vera.model_binding_core import binding as _bind   # type: ignore

_SERVES_FRAME = _bind(
    "frame", "nlp", io_in="text", io_out="labels", tier=1, placement="nlp",
    uses="Frame a text before anything else runs: patterns (urls, paths, tickers, nodes, capability "
         "names), the nearest domains by MiniLM against domain cards, optionally entities and language.",
    limits="understanding meaning beyond domains and patterns (it routes, it does not answer).")


try:                                     # pragma: no cover - import-shape only
    from Vera.vera.minds import contract_core as _MC
except Exception:                        # worktree / app-free import
    from vera.minds import contract_core as _MC  # type: ignore


@capability(
    "sense.frame", serves=_SERVES_FRAME, http_method="POST", http_path="/sense/frame", http_tags=["minds"],
    contract=_MC.contract("sense.frame", effects=["read", "model"], idempotency="idempotent"),
    memory="off", silent=True,
    description="One Frame per text (four-minds FM-1.1): regex patterns (urls, paths, tickers, nodes, "
                "sessions, capability names), the nearest domains by the decision-space vector (MiniLM "
                "on an NLP node) against domain cards, and optionally entities (ner=true; too slow for a "
                "chat's first token) and the language (non-ASCII text only). Cached for an hour by text "
                "hash; patterns-only with degraded=true when the nodes are down. Input: text, kind (chat | "
                "goal | step | page), ner (bool, default false), vector (bool, default false: include the "
                "384-d vector). Output: Frame v0 {schema, text_hash, kind, patterns, domains, domain, "
                "entities, lang, degraded, ms[, vector]}.",
)
async def cap_sense_frame(text: str = "", kind: str = "chat", ner: bool = False, vector: bool = False,
                          trace_id=None) -> Dict[str, Any]:
    t0 = time.monotonic()
    text = str(text or "")[:4000]
    kind = str(kind or "chat")[:20]
    _STATS["calls"] += 1
    h = SC.text_hash(text, kind)
    r = _redis()
    if r is not None and not ner:
        try:
            raw = await r.get(KEY_FRAME + h)
            if raw:
                fr = json.loads(raw)
                if vector:
                    v = await r.get(KEY_VEC + h)
                    fr["vector"] = json.loads(v) if v else None
                fr["cached"] = True
                _STATS["cached"] += 1
                return fr
        except Exception:
            pass
    ms: Dict[str, float] = {}
    pats = SC.patterns(text, CAPABILITY_REGISTRY.keys())
    ms["patterns"] = round((time.monotonic() - t0) * 1000, 2)
    t1 = time.monotonic()
    emb, cards = await asyncio.gather(_raw("nlp.embed", EMBED_TIMEOUT_S, texts=[text]), _cards())
    vecs = emb.get("embeddings") if isinstance(emb.get("embeddings"), list) else []
    vec = vecs[0] if vecs and isinstance(vecs[0], list) else None
    ms["vector"] = round((time.monotonic() - t1) * 1000, 1)
    doms = SC.domains(vec, cards) if vec else []
    degraded = vec is None
    ents: List[Dict[str, Any]] = []
    if ner and text.strip():
        t2 = time.monotonic()
        nr = await _raw("nlp.ner", NER_TIMEOUT_S, text=text)
        # the node's entity shape: {"entity": label, "word": text, "score", "start", "end"}
        ents = [{"text": e.get("word") or e.get("text"),
                 "label": e.get("entity") or e.get("entity_group") or e.get("label"),
                 "score": e.get("score")}
                for e in (nr.get("entities") or []) if isinstance(e, dict)][:40]
        ms["ner"] = round((time.monotonic() - t2) * 1000, 1)
    lang: Optional[str] = "en"
    if SC.non_ascii(text):
        lr = await _raw("nlp.langid", LANGID_TIMEOUT_S, text=text)
        lang = lr.get("lang") or lr.get("language") or None
    ms["total"] = round((time.monotonic() - t0) * 1000, 1)
    fr = SC.frame(text, kind, pats, doms=doms, entities=ents, lang=lang, degraded=degraded, ms=ms,
                  cards_version=SC.card_hash(SC.DOMAIN_CARDS))
    if degraded:
        _STATS["degraded"] += 1
    if r is not None and not degraded:
        try:
            await r.set(KEY_FRAME + h, json.dumps(fr), ex=FRAME_TTL_S)
            await r.set(KEY_VEC + h, json.dumps(vec), ex=FRAME_TTL_S)
        except Exception:
            pass
    if _STATS["calls"] % SAMPLE_EVERY == 1:
        try:
            await emit_event({"type": "sense.frame.timing", "kind": kind, "degraded": degraded, **ms})
        except Exception:
            pass
    if vector:
        fr = dict(fr, vector=vec)
    return fr


@capability(
    "sense.stats", http_method="GET", http_path="/sense/stats", http_tags=["minds"],
    contract=_MC.contract("sense.stats", effects=["read"], idempotency="idempotent"),
    memory="off", silent=True,
    description="sense.frame counters in this process (calls, cached, degraded) and the domain cards in use. "
                "Output: {stats, cards: {domain: text}, cards_version, cards_embedded}.",
)
async def cap_sense_stats(trace_id=None) -> Dict[str, Any]:
    h = SC.card_hash(SC.DOMAIN_CARDS)
    return {"stats": dict(_STATS), "cards": dict(SC.DOMAIN_CARDS), "cards_version": h,
            "cards_embedded": h in _CARDS}
