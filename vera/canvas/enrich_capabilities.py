"""canvas.enrich - bring forth what else on the estate is relevant to a turn, onto its session canvas.

The chat asks for this after a reply lands (and anyone may ask for it with a text): enrich_core decides which
reads fit the turn - the query reads its words allow (market overview + news for a markets question, a web
search for a question about the world or about now, the fabric's records always), then the dashboard reads
caps.search finds relevant that are safe to run unasked - and this runs them, in parallel, each with a time
limit, and lands each answer as an item: a dashboard read as the widget widget.from_result picks for it, a list
as a `records` browser. Items are keyed so asking twice brings them back instead of doubling them, and anchored
to the turn with from='enrich' so the canvas can tell them from what the reply itself carried.

No model is called: caps.search and the fabric embed on the CPU nodes, the rest are plain reads.

Loaded after canvas/, widgets/, web/, fabric/ and markets/: it calls them through the capability registry.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any, Dict, List

from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY, capability

try:
    from Vera.vera.canvas import enrich_core as ec
except ImportError:                                   # pragma: no cover
    from vera.canvas import enrich_core as ec

log = logging.getLogger("vera.canvas.enrich")

STEP_TIMEOUT_S = 15.0


async def _call(name: str, **kw) -> Any:
    cap = CAPABILITY_REGISTRY.get(name)
    if not cap or not cap.get("func"):
        return {"error": f"capability not available: {name}"}
    try:
        return await cap["func"](**kw)
    except Exception as e:
        return {"error": f"{name}: {e}"}


def _sources_mod():
    return sys.modules.get("widget_sources")


def _is_empty(res: Any) -> bool:
    if res is None or res == "" or res == [] or res == {}:
        return True
    if isinstance(res, dict):
        if res.get("error") and len(res) <= 3:
            return True
        for k in ("results", "headlines", "sources", "items", "records", "rows"):
            if k in res and isinstance(res[k], list) and not res[k] and len(res) <= 6:
                return True
    return False


async def _widget_content(cap: str, args: Dict[str, Any], res: Any, title: str) -> Dict[str, Any]:
    """The same widget content the chat's capview adapter lands: the record, named as the widget and its form."""
    fr = await _call("widget.from_result", cap=cap, result=res, args=args, title=title, max=1)
    recs = (fr or {}).get("records") or [] if isinstance(fr, dict) else []
    if not recs:
        return {}
    r = recs[0]
    # a table IS a way to look through rows (sort, page, search); a JSON tree or a bare record is not a glance
    if str(r.get("form") or "") in ("json", "text", "kv", "record", "markdown", "error"):
        return {}
    return dict(r, widget=r.get("form"), form=r.get("form"), title=title, record=r)


@capability(
    "canvas.enrich", memory="off",
    http_method="POST", http_path="/canvas/enrich", http_tags=["canvas"],
    description="Bring forth what ELSE is relevant to a turn onto its session canvas - no model called. From the "
                "turn's words it picks: the market overview and news (a markets question), a web search (a question "
                "about the world or about now), the fabric's records on it (always, over its relevance floor), and "
                "the dashboard reads caps.search finds relevant that are safe to run unasked (measured argument-free "
                "reads whose name writes nothing). Runs them in parallel, each time-limited, and lands each answer: a "
                "dashboard read as its widget, a list as a `records` browser (paged, searchable, sortable). Keyed "
                "(enrich:<cap>[:<hash>]) so asking again recalls instead of doubling; anchored to the turn with "
                "from='enrich'. Inputs: session_id (str!), text (str! - the turn: what was asked), reply (str - "
                "what was answered; lends words when the ask is short), mid (str - the turn's message id), apply "
                "(bool=true - false returns the plan only), max_widgets (int=3), sources (str='caps,query,memory'), "
                "min_score (float=6.5 - on caps.search's scale; a dashboard read also needs 60% of the best one's score). Output: {ok, query, gates, "
                "items:[{key, kind, cap, why, resolved}], skipped:[{cap, why}], ms}.")
async def cap_canvas_enrich(session_id: str = "", text: str = "", reply: str = "", mid: str = "",
                            apply: Any = True, max_widgets: int = 3, sources: str = "caps,query,memory",
                            min_score: float = 6.5, trace_id=None):
    t0 = time.monotonic()
    text = str(text or "").strip()
    if not text:
        return {"ok": False, "error": "text required - the turn's words"}
    do_apply = apply if isinstance(apply, bool) else str(apply).strip().lower() not in ("0", "false", "no")
    if do_apply and not session_id:
        return {"ok": False, "error": "session_id required to land items (or apply=false for the plan)"}
    # a turn with nothing specific in it ("thanks", "go on") brings nothing forth - not even words borrowed from
    # the reply: enrichment runs after every turn, and a read nobody asked about is noise on the canvas
    if not ec.specific_terms(text) and not any(ec.gates(text)[g] for g in ("finance", "fresh", "world")):
        return {"ok": True, "query": "", "items": [], "skipped": [{"cap": "", "why": "nothing specific in the turn"}],
                "ms": int((time.monotonic() - t0) * 1000)}
    words = text if len(ec.terms(text)) >= 2 else (text + " " + str(reply or "")[:300])
    src = [s.strip() for s in str(sources or "").split(",") if s.strip()] or ["caps", "query", "memory"]
    ws = _sources_mod()
    measured = getattr(ws, "MEASURED", {}) if ws else {}
    is_read = getattr(ws, "is_read", None) if ws else None
    found: List[Dict[str, Any]] = []
    if "caps" in src and measured and is_read:
        cs = await _call("caps.search", query=ec.topic(words) or words, top_k=30)
        found = list((cs or {}).get("results") or []) if isinstance(cs, dict) else []
        # caps.search answers name + score; the ranking and the item's "why" read the capability's own words
        for f in found:
            reg = CAPABILITY_REGISTRY.get(str(f.get("name") or "")) or {}
            f.setdefault("description", str(reg.get("description") or ""))
    p = ec.plan(words, found, measured, is_read or (lambda n: False), max_widgets=max(0, int(max_widgets or 0)),
                sources=src, min_score=float(min_score or 6.5))
    if not do_apply:
        return {"ok": True, "query": p["query"], "gates": p["gates"], "plan": p["steps"],
                "ms": int((time.monotonic() - t0) * 1000)}

    async def run(step):
        try:
            res = await asyncio.wait_for(_call(step["cap"], **step["args"]), STEP_TIMEOUT_S)
        except asyncio.TimeoutError:
            return step, {"error": "timed out after %ds" % STEP_TIMEOUT_S}
        return step, res
    done = await asyncio.gather(*[run(s) for s in p["steps"]])

    items, skipped = [], []
    anchor = {"turn": mid, "mid": mid, "from": "enrich"} if mid else {"from": "enrich"}
    # the news feed is a web search underneath: when the two answers are mostly the same pages, land the news
    # (it is the one the turn's subject asked for) and say why the web list is not there
    by = {s["cap"]: r for s, r in done}
    if "web.search" in by and "markets.news.feed" in by:
        ov = ec.overlap(ec.records_from("web.search", by["web.search"]), ec.records_from("markets.news.feed", by["markets.news.feed"]))
        if ov >= 0.6:
            done = [(s, r) for s, r in done if s["cap"] != "web.search"]
            skipped.append({"cap": "web.search", "why": "the same pages as the news (%d%% overlap)" % round(ov * 100)})
    for step, res in done:
        cap, args, kind = step["cap"], step["args"], step["kind"]
        if isinstance(res, dict) and res.get("error") and _is_empty({k: v for k, v in res.items() if k != "error"} or None):
            skipped.append({"cap": cap, "why": str(res.get("error"))[:200]})
            continue
        if _is_empty(res):
            skipped.append({"cap": cap, "why": "answered with nothing"})
            continue
        if kind == "widget":
            title = cap.replace(".", " · ")
            content = await _widget_content(cap, args, res, title)
            block, size = "widget", "m"
            if not content:
                rec = ec.records_item(cap, args, res, kind="rows", query=p["query"], why=step["why"], title=title)
                if not rec:
                    skipped.append({"cap": cap, "why": "no widget and no list in its answer"})
                    continue
                content, block, size = rec, "records", "l"
            else:
                content["why"] = step["why"]
        else:
            rec = ec.records_item(cap, args, res, kind=kind, query=p["query"], why=step["why"])
            if not rec:
                skipped.append({"cap": cap, "why": "no records in its answer"})
                continue
            content, block, size = rec, "records", "l"
        key = ec.item_key(cap, args, kind)
        added = await _call("canvas.add", session_id=session_id, kind=block, content=content, key=key,
                            at="now", size=size, anchor=anchor)
        if isinstance(added, dict) and added.get("resolved") == "shown" and block == "records":
            # the same query again: its answer may have moved - the item shows the fresh one
            await _call("canvas.update", session_id=session_id, key=key, content=content)
        items.append({"key": key, "kind": block, "cap": cap, "why": step["why"],
                      "resolved": (added or {}).get("resolved") if isinstance(added, dict) else None,
                      "error": (added or {}).get("error") if isinstance(added, dict) else None})
    return {"ok": True, "query": p["query"], "gates": p["gates"], "items": items, "skipped": skipped,
            "ms": int((time.monotonic() - t0) * 1000)}


# ─────────────────────────────────────────────────────────────────────────────
# WHAT A TURN NAMES (canvas-enrichment plan, slice 1 - owner, 2026-10-03: "the nlp enrichment is still few and far
# between"). The NLP tier's entity model is live on the CPU nodes and was never called on a reply. turn.understand
# reads a reply (or any text) with it, chunk by chunk in parallel, merges the mentions with the patterns turn_core
# knows (tickers, links), and - when there is enough to be worth an item - lands them on the session canvas as one
# `entities` item anchored to the turn. turn.entity.look is what a chip does when pressed: the fabric's records and
# a web search for that one name, as records browsers beside it. No model is called; nothing touches the GPU.
# ─────────────────────────────────────────────────────────────────────────────

try:
    from Vera.vera.canvas import turn_core as tc
except ImportError:                                   # pragma: no cover
    from vera.canvas import turn_core as tc

NER_PARALLEL = 4
NER_TIMEOUT_S = 12.0


async def _ner(text: str) -> List[Any]:
    parts = tc.chunks(text)
    sem = asyncio.Semaphore(NER_PARALLEL)

    async def one(off, chunk):
        async with sem:
            try:
                r = await asyncio.wait_for(_call("nlp.ner", text=chunk), NER_TIMEOUT_S)
            except asyncio.TimeoutError:
                r = {"error": "timed out"}
            return off, (r if isinstance(r, dict) else {})
    return list(await asyncio.gather(*[one(o, c) for o, c in parts[:12]]))


@capability(
    "turn.understand", memory="off",
    http_method="POST", http_path="/turn/understand", http_tags=["canvas", "nlp"],
    description="What a turn NAMES: the people, organisations, places, products, events, money, dates, tickers and "
                "links in a text (a reply, a report, a question) - the NLP tier's entity model (nlp.ner, CPU nodes) "
                "read chunk by chunk, merged with patterns, one entry per name with how often it was named and the "
                "sentences it was named in. place=true lands them on the session canvas as one `entities` item "
                "(keyed by the turn) when at least `min_entities` things are named. No model is called. Inputs: text "
                "(str!), session_id (str), mid (str - the turn's message id), place (bool=false), min_entities "
                "(int=3), subject (str - the item's title). Output: {ok, entities:[{text, type, group, count, score, "
                "sentences}], salient:int, placed?:{key, resolved}, ner:{chunks, failed}, ms}.")
async def cap_turn_understand(text: str = "", session_id: str = "", mid: str = "", place: bool = False,
                              min_entities: int = 3, subject: str = "", trace_id=None) -> Dict[str, Any]:
    t0 = time.monotonic()
    body = tc.prose(text)
    if not body.strip():
        return {"ok": False, "error": "text required"}
    tagged = await _ner(body)
    failed = [str((a or {}).get("error"))[:120] for _, a in tagged if (a or {}).get("error")]
    ents = tc.merge(body, tagged)
    out: Dict[str, Any] = {"ok": True, "entities": ents, "salient": len(tc.salient(ents)),
                           "ner": {"chunks": len(tagged), "failed": failed[:3]}}
    if place and session_id and len(tc.salient(ents)) >= max(1, int(min_entities or 3)):
        key = "entities:" + (mid or str(abs(hash(body)) % 10 ** 10))
        anchor = {"turn": mid, "mid": mid, "from": "enrich"} if mid else {"from": "enrich"}
        added = await _call("canvas.add", session_id=session_id, kind="entities", key=key, at="now", size="m",
                            content=tc.item(ents, mid=mid, subject=subject), anchor=anchor)
        out["placed"] = {"key": key, "resolved": (added or {}).get("resolved") if isinstance(added, dict) else None,
                         "error": (added or {}).get("error") if isinstance(added, dict) else None}
    out["ms"] = int((time.monotonic() - t0) * 1000)
    return out


@capability(
    "turn.entity.look", memory="off",
    http_method="POST", http_path="/turn/entity/look", http_tags=["canvas", "nlp"],
    description="Look one named thing up and put what is found beside it on the session canvas: the fabric's records "
                "for it and (for anything outside the estate - a person, an organisation, a place, a product, an "
                "event) a web search, each as a `records` browser keyed by the name. No model is called. Inputs: "
                "session_id (str!), text (str! - the name), type (str - its entity type), mid (str), web (bool - "
                "default by type). Output: {ok, items:[{key, cap, resolved}], skipped:[{cap, why}]}.")
async def cap_turn_entity_look(session_id: str = "", text: str = "", type: str = "", mid: str = "",
                               web: Any = None, trace_id=None) -> Dict[str, Any]:
    name = str(text or "").strip()
    if not session_id or not name:
        return {"ok": False, "error": "session_id and text required"}
    typ = str(type or "").upper()
    use_web = bool(web) if web is not None else typ in ("PERSON", "ORG", "GPE", "LOC", "NORP", "FAC", "PRODUCT",
                                                         "EVENT", "WORK_OF_ART", "LAW", "TICKER", "")
    steps = [("fabric.query", {"text": name, "top_k": 30}, "memory", "what the fabric holds on " + name)]
    if use_web:
        steps.append(("web.search", {"query": name, "limit": 20}, "web", "a web search for " + name))
    anchor = {"turn": mid, "mid": mid, "from": "enrich"} if mid else {"from": "enrich"}

    async def run(cap, args):
        try:
            return await asyncio.wait_for(_call(cap, **args), STEP_TIMEOUT_S)
        except asyncio.TimeoutError:
            return {"error": "timed out after %ds" % STEP_TIMEOUT_S}
    done = await asyncio.gather(*[run(c, a) for c, a, _, _ in steps])
    items, skipped = [], []
    for (cap, args, kind, why), res in zip(steps, done):
        if _is_empty(res):
            skipped.append({"cap": cap, "why": (res or {}).get("error") if isinstance(res, dict) and res.get("error") else "found nothing"})
            continue
        rec = ec.records_item(cap, args, res, kind=kind, query=name, why=why, title=name + " \u00b7 " + ("the fabric" if kind == "memory" else "the web"))
        if not rec:
            skipped.append({"cap": cap, "why": "no records in its answer"})
            continue
        key = "entity:" + ec.item_key(cap, args, kind)
        added = await _call("canvas.add", session_id=session_id, kind="records", content=rec, key=key, at="now",
                            size="l", anchor=anchor)
        items.append({"key": key, "cap": cap, "resolved": (added or {}).get("resolved") if isinstance(added, dict) else None})
    return {"ok": True, "items": items, "skipped": skipped}


# ─────────────────────────────────────────────────────────────────────────────
# THE SURFACE INDEX (canvas-enrichment slice 3): what each part of the estate's UI can show, by domain - the reads that
# fit a turn, the dashboard widgets already drawn over them, the panels - and what the turn names that fills their
# arguments. surface.find is what the before-the-model step (slice 4) asks; it calls nothing but the NLP tier.
# ─────────────────────────────────────────────────────────────────────────────

try:
    from Vera.vera.canvas import surface_core as sc
except ImportError:                                   # pragma: no cover
    from vera.canvas import surface_core as sc

import json as _json
import os as _os

_SURF: Dict[str, Any] = {"at": 0.0, "index": None}
SURFACE_TTL_S = 600.0


def _layouts() -> List[Dict[str, Any]]:
    d = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "widgets", "layouts")
    out = []
    try:
        for n in sorted(_os.listdir(d)):
            if n.endswith(".json"):
                try:
                    with open(_os.path.join(d, n), encoding="utf-8") as fh:
                        out.append(_json.load(fh))
                except (OSError, ValueError):
                    continue
    except OSError:
        pass
    return out


async def _surface_index(refresh: bool = False) -> Dict[str, Any]:
    if not refresh and _SURF["index"] is not None and time.monotonic() - _SURF["at"] < SURFACE_TTL_S:
        return _SURF["index"]
    ws = _sources_mod()
    pl = await _call("ui.panel.list")
    panels = (pl or {}).get("panels") or [] if isinstance(pl, dict) else []
    idx = sc.build(CAPABILITY_REGISTRY, getattr(ws, "MEASURED", {}) if ws else {}, _layouts(), panels)
    _SURF.update(at=time.monotonic(), index=idx)
    return idx


@capability(
    "surface.index", memory="off", silent=True,
    http_method="GET", http_path="/surface/index", http_tags=["canvas"],
    description="The surface index: what each part of the estate's UI can show, by domain (markets, your trading "
                "account, your business accounts, the estate, the diary, research) - every read that only reads, with "
                "the kinds of thing its parameters take (a symbol, a url, a node, a path, a dataset); every dashboard "
                "widget over such a read; every panel. Built from the registry, the measured widget sources, the "
                "layouts and the panel list; kept 10 minutes. Inputs: refresh (bool), domain (str - list that domain's "
                "surfaces). Output: {counts:{domain:{read, widget, panel}}, domains:[{id, label}], surfaces?:[...]}.")
async def cap_surface_index(refresh: bool = False, domain: str = "", trace_id=None) -> Dict[str, Any]:
    idx = await _surface_index(bool(refresh))
    out: Dict[str, Any] = {"counts": idx["counts"], "domains": [{"id": d["id"], "label": d["label"]} for d in sc.DOMAINS]}
    if domain:
        out["surfaces"] = [{k: v for k, v in s.items() if k != "record"} for s in idx["surfaces"] if s["domain"] == domain][:300]
    return out


@capability(
    "surface.find", memory="off",
    http_method="POST", http_path="/surface/find", http_tags=["canvas"],
    description="Which surfaces answer a turn: reads the turn's words and the things it names (turn_core patterns, and "
                "the NLP tier's entities when ner=true), scores the domains, and answers - for the top ones - the "
                "dashboard widgets, the glance reads, the reads the named things fill (a ticker into a symbol "
                "parameter...) and the panels, each with its arguments and why. The two accounts are kept apart: a "
                "turn that only says 'my account' gets both, named apart. No model is called. Inputs: text (str!), "
                "entities (list - from turn.understand, else found here), ner (bool=false), max_items (int=6). Output: "
                "{domains:[{id, label, score}], surfaces:[{kind, cap?, panel?, args, domain, domain_label, why, record?}], ms}.")
async def cap_surface_find(text: str = "", entities: Any = None, ner: bool = False, max_items: int = 6,
                           trace_id=None) -> Dict[str, Any]:
    t0 = time.monotonic()
    if not str(text or "").strip():
        return {"ok": False, "error": "text required"}
    ents = list(entities) if isinstance(entities, list) else None
    if ents is None:
        body = tc.prose(text)
        ents = tc.merge(body, await _ner(body)) if ner else tc.merge(body, [])
    idx = await _surface_index()
    out = sc.find(idx, text, ents, max_items=max(1, int(max_items or 6)))
    out.update(ok=True, ms=int((time.monotonic() - t0) * 1000))
    return out
