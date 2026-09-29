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
