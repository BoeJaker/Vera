"""
intel_capabilities.py - Intelligence: tracked topics, their timeline, and reports
==================================================================================

Follows complex topics over time. A topic is swept for new signals (web search
through fabric discovery, so every page also lands in the topic's own discovery
dataset and entity graph), the strongest signals are read in reader mode and
assessed, and a snapshot records what Vera believes about the topic now and
what changed since the last one. The run of snapshots and signals is the
topic's timeline.

Reports are built from that state and rendered once per channel: a
self-contained HTML page (reader-mode digests, images, diagrams, highlighted
code and buttons that let Vera act - fetch a repo, research a question further,
track an entity as its own topic), Telegram with buttons, chat, email, canvas,
notebook, memory, podcast or the printer.

Background work follows the house rule: once a night, capped, through the idle
queue as `job_type: idle_intel` with `prefer_gpu: false`; the morning brief is
only delivered at brief time, never generated then.

Woven into: fabric discovery (sweeps), the dream director (a briefing section
and an `intel` delivery channel that turns dream output into signals), the
narrator (a probe), the canvas and widgets (readable caps, a layout, an
element), and places (`intel`).

Replaces the `intel-*` n8n flows: `intel.import.n8n` brings their topics and
enriched items over; `intel.n8n.retire` switches the flows off (dry run first).
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Request
from fastapi.responses import HTMLResponse, Response

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, CAPABILITY_REGISTRY, capability, emit_event, enum_schema, register_ui, schedule,
)
from Vera.vera.intel import intel_core as core
from Vera.vera.intel.intel_store import IntelStore

log = logging.getLogger("vera.intel")

_HERE = Path(__file__).parent
_PANEL = _HERE / "intel_panel.html"
_TIMELINE_JS = _HERE / "intel_timeline_element.js"

KIND_INTEL = "intel"
SIGNALS_DATASET = "intel.signals"

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,              # the nightly refresh + morning brief
    "timezone": "Europe/London",
    "sweep_hour": 1,              # the nightly refresh is queued from this hour (idle queue runs it when quiet)
    "brief_hour": 7,              # the brief is delivered from this hour
    "max_topics_per_night": 6,
    "report_period_hours": 24,
    "brief_channels": ["html", "telegram"],
    "public_base": "https://llm.int:8999",   # links in Telegram / email must work off the box
    "illustrate": False,
}

_STORE: Optional[IntelStore] = None


def store() -> IntelStore:
    global _STORE
    if _STORE is None:
        from Vera.vera.state_paths import state_dir
        _STORE = IntelStore(state_dir("intel") / "intel.db")
    return _STORE


def config() -> Dict[str, Any]:
    c = dict(DEFAULT_CONFIG)
    c.update(store().get_kv("config", {}) or {})
    return c


# ── plumbing ─────────────────────────────────────────────────────────────────

def _has(name: str) -> bool:
    e = CAPABILITY_REGISTRY.get(name)
    return bool(isinstance(e, dict) and (e.get("func") or e.get("raw")))


async def _call(name: str, **kw) -> Dict[str, Any]:
    """Call a capability in-process, dropping arguments it does not take; a missing
    capability or a raise comes back as {error} instead of breaking the caller."""
    e = CAPABILITY_REGISTRY.get(name)
    fn = isinstance(e, dict) and (e.get("func") or e.get("raw"))
    if not fn:
        return {"error": f"{name} unavailable"}
    try:
        params = inspect.signature(e.get("raw") or fn).parameters
        if not any(p.kind == p.VAR_KEYWORD for p in params.values()):
            kw = {k: v for k, v in kw.items() if k in params}
    except (TypeError, ValueError):
        pass
    try:
        r = await fn(**kw)
    except Exception as ex:  # noqa: BLE001 - a sibling subsystem failing must not take intel down
        log.warning("intel: %s failed: %s", name, ex)
        return {"error": f"{type(ex).__name__}: {ex}"}
    if isinstance(r, dict) and "content" in r and r.get("type") == "tool_result":
        r = r["content"]
    return r if isinstance(r, dict) else {"result": r}


async def _llm_json(prompt: str, *, job: str = "idle_intel", timeout: float = 900.0) -> Optional[Dict[str, Any]]:
    """One background model call (CPU-preferred, never thinking aloud) parsed as JSON."""
    gen = getattr(_orch, "ollama_generate", None)
    if gen is None:
        return None
    try:
        raw = await gen(prompt, json_mode=True, think=False, prefer_gpu=False, job_type=job, timeout=timeout)
    except Exception as ex:  # noqa: BLE001
        log.warning("intel: model call failed: %s", ex)
        return None
    p = core.parse_json_loose(raw or "")
    return p if isinstance(p, dict) else None


async def _event(kind: str, **fields: Any) -> None:
    try:
        await emit_event({"type": f"intel.{kind}", **fields})
    except Exception:  # noqa: BLE001
        pass


def _since(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _list_arg(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    s = str(v).strip()
    if s.startswith("["):
        try:
            return [str(x).strip() for x in json.loads(s) if str(x).strip()]
        except ValueError:
            pass
    return [x.strip() for x in re.split(r"[,\n]", s) if x.strip()]


def _feed_row(s: Dict[str, Any], topics: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """A signal as the widgets' `events` shape (feed / lane / log forms)."""
    t = topics.get(s.get("topic_id", ""), {})
    sc = s.get("score")
    return {"kind": t.get("name") or s.get("topic_id"), "title": s.get("title"),
            "body": s.get("summary") or s.get("snippet") or "", "who": s.get("site") or s.get("source"),
            "when": s.get("seen"), "t": s.get("seen"), "text": s.get("title"), "url": s.get("url"),
            "score": sc, "level": "hi" if (sc or 0) >= 7 else "", "id": s.get("id"), "topic_id": s.get("topic_id")}


def _topic_stats(t: Dict[str, Any]) -> Dict[str, Any]:
    st = store()
    sn = st.latest_snapshot(t["id"])
    return {"signals_24h": st.count_signals(since=_since(24), topic_id=t["id"]),
            "signals_total": st.count_signals(topic_id=t["id"]),
            "headline": (sn or {}).get("headline", ""), "confidence": (sn or {}).get("confidence", ""),
            "snapshot_ts": (sn or {}).get("ts", ""), "due": core.is_due(t)}


def _report_url(rid: str, *, public: bool = False) -> str:
    base = config().get("public_base", "").rstrip("/") if public else ""
    return f"{base}/intel/report/{rid}"


# ── topics ───────────────────────────────────────────────────────────────────

@capability("intel.topic.list", http_method="GET", http_path="/intel/topic/list", http_tags=["intel"],
            memory="off", silent=True,
            description="List tracked intelligence topics with live stats (signals in 24h, latest headline, due). "
                        "Input: status (active|paused|archived, blank = all). Output: {rows:[topic+stats], count}.")
async def cap_topic_list(status: str = "", trace_id=None):
    rows = []
    for t in store().topics(status):
        stt = _topic_stats(t)
        rows.append({**t, **stt, "meta": f"{stt['signals_24h']} new" + (f" · {stt['headline'][:80]}" if stt["headline"] else "")})
    return {"rows": rows, "count": len(rows)}


@capability("intel.topic.get", http_method="GET", http_path="/intel/topic/get", http_tags=["intel"],
            memory="off", silent=True,
            description="One tracked topic: the record, its latest assessment and stats. Input: id. "
                        "Output: {topic, snapshot, stats, queries}.")
async def cap_topic_get(id: str = "", trace_id=None):
    t = store().get_topic(id)
    if not t:
        return {"error": f"unknown topic: {id}"}
    return {"topic": t, "snapshot": store().latest_snapshot(id), "stats": _topic_stats(t),
            "queries": core.topic_queries(t)}


@capability("intel.topic.save", http_method="POST", http_path="/intel/topic/save", http_tags=["intel"],
            schema=enum_schema(depth=list(core.DEPTHS), cadence=list(core.CADENCES), status=list(core.STATUSES)),
            description="Create or update a tracked topic. Input: name (str!), id (to update), brief (what it is), "
                        "why (why it matters), questions / queries / entities / exclude / sites (lists or lines), "
                        "depth (scan|standard|deep), cadence, status, deliver (channels), min_score, dream_loop "
                        "(also run a dream think-loop on it), parent. Output: {ok, topic}.")
async def cap_topic_save(name: str = "", id: str = "", brief: str = None, why: str = None,
                         questions: Any = None, queries: Any = None, entities: Any = None,
                         exclude: Any = None, sites: Any = None, depth: str = "", cadence: str = "",
                         status: str = "", deliver: Any = None, min_score: float = None,
                         dream_loop: bool = None, discovery: bool = None, parent: str = "",
                         color: str = "", origin: str = "", trace_id=None):
    st = store()
    existing = st.get_topic(id) if id else (st.get_topic(core.slug(name)) if name else None)
    raw: Dict[str, Any] = {"name": name, "id": id, "depth": depth, "cadence": cadence, "status": status,
                           "parent": parent, "color": color, "origin": origin}
    for k, v in (("brief", brief), ("why", why), ("min_score", min_score), ("dream_loop", dream_loop),
                 ("discovery", discovery)):
        if v is not None:
            raw[k] = v
    for k, v in (("questions", questions), ("queries", queries), ("entities", entities),
                 ("exclude", exclude), ("sites", sites), ("deliver", deliver)):
        if v is not None:
            raw[k] = _list_arg(v)
    raw = {k: v for k, v in raw.items() if v not in ("", None) or k in ("brief", "why")}
    try:
        t = core.normalise_topic(raw, existing)
    except ValueError as e:
        return {"error": str(e)}
    st.put_topic(t)
    if t.get("dream_loop"):
        await _ensure_think_loop(t)
    await _event("topic.saved", topic=t["id"], name=t["name"], created=existing is None)
    return {"ok": True, "topic": t, "created": existing is None}


@capability("intel.topic.delete", http_method="POST", http_path="/intel/topic/delete", http_tags=["intel"],
            description="Stop tracking a topic. Input: id, purge (also drop its signals and snapshots; "
                        "default false keeps the history). Output: {ok}.")
async def cap_topic_delete(id: str = "", purge: bool = False, trace_id=None):
    if not id:
        return {"error": "id required"}
    ok = store().delete_topic(id, purge=purge)
    await _event("topic.deleted", topic=id, purge=purge)
    return {"ok": ok}


async def _ensure_think_loop(t: Dict[str, Any]) -> None:
    """A deep topic can also be a dream think-loop, so the director may choose to
    think about it between sweeps; its journal comes back as dream signals."""
    st = store()
    name = f"think_intel_{t['id'].replace('-', '_')}"[:60]
    loops = st.get_kv("think_loops", {}) or {}
    if loops.get(t["id"]) == name or not _has("dream.think.create"):
        return
    goal = (f"Keep an evolving understanding of {t['name']}. {t.get('brief', '')} "
            f"Why it matters: {t.get('why') or 'see brief'}. Open questions: "
            + "; ".join((t.get("questions") or [])[:5]))
    r = await _call("dream.think.create", subject=t["name"], goal=goal[:1500], name=name,
                    interval_minutes=720, idle_minutes=30)
    if not r.get("error"):
        loops[t["id"]] = name
        st.set_kv("think_loops", loops)


# ── signals and the timeline ─────────────────────────────────────────────────

@capability("intel.signal.list", http_method="GET", http_path="/intel/signal/list", http_tags=["intel"],
            memory="off", silent=True,
            description="Signals for one topic or all, newest first (or by score). Input: topic_id, hours "
                        "(window, default 168), min_score, limit, order (seen|score). Output: {events:[feed rows], "
                        "rows:[reading rows], count} - ready for the feed / reading widgets.")
async def cap_signal_list(topic_id: str = "", hours: float = 168, min_score: float = -1, limit: int = 60,
                          order: str = "seen", trace_id=None):
    topics = {t["id"]: t for t in store().topics()}
    sigs = store().signals(topic_id, since=_since(hours) if hours else "", limit=max(1, min(500, int(limit))),
                           min_score=None if min_score is None or min_score < 0 else float(min_score), order=order)
    return {"events": [_feed_row(s, topics) for s in sigs],
            "rows": [{"title": s.get("title"), "url": s.get("url"), "snippet": s.get("summary") or s.get("snippet"),
                      "chars": len((s.get("digest") or {}).get("text") or ""), "score": s.get("score"),
                      "id": s.get("id"), "topic_id": s.get("topic_id"), "image": s.get("image"),
                      "kind": s.get("kind"), "site": s.get("site"), "seen": s.get("seen"),
                      "so_what": s.get("so_what"), "tags": s.get("tags") or []} for s in sigs],
            "count": len(sigs)}


@capability("intel.signal.get", http_method="GET", http_path="/intel/signal/get", http_tags=["intel"],
            memory="off", silent=True,
            description="One signal in full (enrichment, reader digest, code). Input: id. Output: {signal}.")
async def cap_signal_get(id: str = "", trace_id=None):
    s = store().get_signal(id)
    return {"signal": s} if s else {"error": f"unknown signal: {id}"}


@capability("intel.signal.add", http_method="POST", http_path="/intel/signal/add", http_tags=["intel"],
            description="Add a signal by hand (a link or a note) to a topic. Input: topic_id (str!), url, title, "
                        "note. Output: {ok, signal}.")
async def cap_signal_add(topic_id: str = "", url: str = "", title: str = "", note: str = "", trace_id=None):
    t = store().get_topic(topic_id)
    if not t:
        return {"error": f"unknown topic: {topic_id}"}
    s = core.normalise_signal(topic_id, {"url": url, "title": title or url or note[:80], "snippet": note,
                                         "kind": "manual" if not url else ""}, source="manual")
    if not s:
        return {"error": "nothing usable to add"}
    s["score"] = 7.0
    store().put_signal(s)
    await _event("signal.new", topic=topic_id, id=s["id"], title=s["title"])
    return {"ok": True, "signal": s}


@capability("intel.timeline.get", http_method="GET", http_path="/intel/timeline/get", http_tags=["intel"],
            memory="off", silent=True,
            description="A topic's timeline (or every topic's): day buckets over the window plus the signals, "
                        "assessments and reports as point events, newest first. Input: topic_id, days "
                        "(default 90). Output: {days:[{date,signals,snapshots,reports,top}], events, from, to}.")
async def cap_timeline_get(topic_id: str = "", days: int = 90, trace_id=None):
    days = max(1, min(730, int(days or 90)))
    since = _since(days * 24)
    st = store()
    tl = core.timeline(st.signals(topic_id, since=since, limit=5000), st.snapshots(topic_id, since=since, limit=1000),
                       st.reports(topic_id=topic_id, since=since, limit=500), days=days)
    for e in tl["events"]:
        if e["kind"] == "report":
            e["url"] = _report_url(e["id"])
    tl["topic_id"] = topic_id
    return tl


@capability("intel.asof.get", http_method="GET", http_path="/intel/asof/get", http_tags=["intel"],
            memory="off", silent=True,
            description="Stand at a moment in a topic's history: the assessment in force then and the signals "
                        "of that day. Input: topic_id (str!), when (ISO date or time). Output: {snapshot, signals}.")
async def cap_asof_get(topic_id: str = "", when: str = "", trace_id=None):
    if not topic_id or not when:
        return {"error": "topic_id and when required"}
    st = store()
    d = core.parse_ts(when)
    if not d:
        return {"error": f"unreadable time: {when}"}
    end = when if "T" in when else f"{when[:10]}T23:59:59Z"
    day0 = f"{end[:10]}T00:00:00Z"
    return {"snapshot": core.as_of(st.snapshots(topic_id, limit=2000), end),
            "signals": st.signals(topic_id, since=day0, until=end, limit=100, order="score"),
            "when": end}


@capability("intel.summary", http_method="GET", http_path="/intel/summary", http_tags=["intel"],
            memory="off", silent=True,
            description="Intelligence at a glance: topics followed, signals in 24h, reports this week, actions "
                        "waiting, last refresh. Output: {values:[{name,value}], ...} for the numbers widget.")
async def cap_summary(trace_id=None):
    st = store()
    active = st.topics("active")
    s24 = st.count_signals(since=_since(24))
    reps = st.reports(since=_since(24 * 7), limit=200)
    acts = st.actions(status="offered", limit=500)
    last = st.get_kv("last_nightly", {}) or {}
    return {"values": [{"name": "topics", "value": len(active)}, {"name": "signals 24h", "value": s24},
                       {"name": "reports 7d", "value": len(reps)}, {"name": "actions open", "value": len(acts)}],
            "topics": len(active), "signals_24h": s24, "reports_7d": len(reps), "actions_open": len(acts),
            "last_refresh": last.get("ts", ""), "enabled": bool(config().get("enabled"))}


@capability("intel.entities.get", http_method="GET", http_path="/intel/entities/get", http_tags=["intel"],
            memory="off", silent=True,
            description="The entities a topic's sources name, as a graph: discovery's entity graph for the topic's "
                        "dataset plus the entities its assessed signals mention. Input: topic_id (str!), limit. "
                        "Output: {nodes, links}.")
async def cap_entities_get(topic_id: str = "", limit: int = 40, trace_id=None):
    t = store().get_topic(topic_id)
    if not t:
        return {"error": f"unknown topic: {topic_id}"}
    counts: Dict[str, int] = {}
    for s in store().signals(topic_id, limit=400):
        for e in s.get("entities") or []:
            counts[e] = counts.get(e, 0) + 1
    links: List[Dict[str, Any]] = []
    g = await _call("fabric.entity_graph.query", dataset_id=f"intel.{topic_id}", limit=int(limit))
    for e in (g.get("entities") or [])[: int(limit)]:
        nm = str(e.get("name") or "").strip()
        if nm:
            counts[nm] = counts.get(nm, 0) + int(e.get("mention_count") or 1)
    for r in g.get("relationships") or []:
        a, b = str(r.get("from_name") or r.get("source") or ""), str(r.get("to_name") or r.get("target") or "")
        if a and b:
            links.append({"source": a, "target": b, "kind": r.get("rel") or r.get("type") or ""})
    top = sorted(counts.items(), key=lambda kv: -kv[1])[: int(limit)]
    names = {n for n, _ in top}
    nodes = [{"id": t["name"], "label": t["name"], "kind": "topic", "weight": 10}] + \
            [{"id": n, "label": n, "kind": "entity", "weight": c} for n, c in top]
    links = [l for l in links if l["source"] in names and l["target"] in names] + \
            [{"source": t["name"], "target": n, "kind": "mentions", "weight": c} for n, c in top]
    return {"nodes": nodes, "links": links, "count": len(nodes)}


# ── collection: sweep (no model), enrich (model), snapshot (model) ───────────

async def sweep_topic(t: Dict[str, Any], *, depth: str = "") -> Dict[str, Any]:
    """Run the topic's next searches through discovery-first web search. No model
    call: signals are scored by keywords, recency and novelty until enriched."""
    st = store()
    budget = core.DEPTH_BUDGET.get(depth or t.get("depth") or "standard", core.DEPTH_BUDGET["standard"])
    queries, cursor = core.rotate_queries(t, budget["queries"])
    recent = [s.get("title", "") for s in st.signals(t["id"], since=_since(24 * 30), limit=400)]
    new: List[Dict[str, Any]] = []
    errors: List[str] = []
    for q in queries:
        r = await _call("web.search", query=q, limit=budget["results"],
                        dataset_id=(f"intel.{t['id']}" if t.get("discovery", True) else ""),
                        discover=("background" if t.get("discovery", True) else "off"))
        if r.get("error"):
            errors.append(f"{q}: {r['error']}")
            continue
        for hit in r.get("results") or []:
            s = core.normalise_signal(t["id"], hit, source="web", query=q)
            if not s or st.has_signal(s["id"]):
                continue
            nv = core.novelty(s, recent)
            if nv < 0.15:                      # the same story under another url
                continue
            s["novelty"] = nv
            s["score"] = core.heuristic_score(s, t, novelty_v=nv)
            st.put_signal(s)
            recent.append(s["title"])
            new.append(s)
    new += await _dream_signals(t)
    t = dict(t, query_cursor=cursor, last_sweep=core.now_iso())
    st.put_topic(t)
    await _to_fabric(new)
    for s in new[:20]:
        await _event("signal.new", topic=t["id"], id=s["id"], title=s["title"], score=s.get("score"))
    return {"topic": t["id"], "queries": queries, "new": len(new), "errors": errors}


async def _dream_signals(t: Dict[str, Any]) -> List[Dict[str, Any]]:
    """What the topic's dream think-loop wrote since the last sweep, as signals."""
    name = (store().get_kv("think_loops", {}) or {}).get(t["id"])
    if not name:
        return []
    r = await _call("dream.think.stream", name=name, limit=10)
    out = []
    for th in (r.get("thoughts") or r.get("entries") or [])[:10]:
        text = str(th.get("text") or th.get("thought") or th.get("content") or "").strip()
        if not text:
            continue
        s = core.normalise_signal(t["id"], {"title": text.split("\n")[0][:140], "snippet": text,
                                            "kind": "dream", "seen": th.get("ts") or th.get("created_at")},
                                  source=f"dream:{name}")
        if s and not store().has_signal(s["id"]):
            s["score"] = 6.0
            store().put_signal(s)
            out.append(s)
    return out


async def _to_fabric(sigs: List[Dict[str, Any]]) -> None:
    """Signals also live in the fabric (one dataset), so search, the worldview and
    discovery see them; embedding is deferred to the backfill, off the hot path."""
    if not sigs:
        return
    fab = sys.modules.get("data_fabric")
    ingest = getattr(fab, "ingest_dataset", None) if fab else None
    if not ingest:
        return
    rows = [{k: s.get(k) for k in ("id", "topic_id", "title", "url", "snippet", "summary", "so_what", "kind",
                                   "site", "seen", "score", "tags", "entities")} for s in sigs]
    try:
        await ingest(dataset_id=SIGNALS_DATASET, data=rows, source="intel", tags=["intel"],
                     source_id="intel", defer_embedding=True)
    except Exception as ex:  # noqa: BLE001
        log.info("intel: fabric ingest skipped: %s", ex)


async def _stop_reason(probe) -> str:
    """The idle queue's probe is async and answers why to STOP ('' = carry on)."""
    if probe is None:
        return ""
    try:
        r = probe()
        if inspect.isawaitable(r):
            r = await r
    except Exception:  # noqa: BLE001
        return ""
    return str(r or "")


async def enrich_topic(t: Dict[str, Any], *, limit: int = 0, should_continue=None) -> Dict[str, Any]:
    """Read the strongest unread signals in reader mode and have a model assess each."""
    st = store()
    n = limit or core.DEPTH_BUDGET.get(t.get("depth") or "standard", core.DEPTH_BUDGET["standard"])["enrich"]
    todo = [s for s in st.signals(t["id"], since=_since(24 * 14), limit=200, enriched=False, order="score")
            if s.get("kind") not in ("dream",)][:n]
    done = 0
    for s in todo:
        if await _stop_reason(should_continue):
            break
        page: Dict[str, Any] = {}
        if s.get("url"):
            r = await _call("reader.page", url=s["url"], max_chars=20000)
            if r.get("ok") or r.get("markdown"):
                page = r
        parsed = await _llm_json(core.enrich_prompt(t, s, page.get("markdown") or ""))
        if not parsed:
            continue
        s2 = core.apply_enrichment(s, parsed, page=page or None)
        st.put_signal(s2)
        done += 1
        await _event("signal.enriched", topic=t["id"], id=s2["id"], title=s2["title"], score=s2.get("score"))
    if done:
        await _to_fabric([st.get_signal(s["id"]) for s in todo if st.get_signal(s["id"])])
    return {"topic": t["id"], "enriched": done, "candidates": len(todo)}


async def snapshot_topic(t: Dict[str, Any]) -> Dict[str, Any]:
    """Update the topic's assessment from the evidence since the last one."""
    st = store()
    prev = st.latest_snapshot(t["id"])
    since = (prev or {}).get("ts") or _since(24 * 7)
    sigs = [s for s in st.signals(t["id"], since=since, limit=60, order="score") if (s.get("score") or 0) >= 3]
    if not sigs and prev:
        return {"topic": t["id"], "skipped": "no new evidence"}
    parsed = await _llm_json(core.snapshot_prompt(t, prev, sigs))
    if not parsed:
        return {"topic": t["id"], "error": "no assessment"}
    snap = core.make_snapshot(t, parsed, sigs)
    st.put_snapshot(snap)
    # answered questions leave the open list; new ones join it
    qs = [q for q in t.get("questions") or [] if q not in {a["question"] for a in snap["answered"]}]
    qs += [q for q in snap["new_questions"] if q not in qs]
    st.put_topic(dict(t, questions=qs[:12], last_snapshot=snap["ts"]))
    await _event("snapshot.new", topic=t["id"], id=snap["id"], headline=snap["headline"],
                 changed=len(snap["changed"]))
    return {"topic": t["id"], "snapshot": snap}


@capability("intel.sweep", http_method="POST", http_path="/intel/sweep", http_tags=["intel"],
            description="Collect new signals now through discovery-first web search (no model call). Input: "
                        "topic_id (blank = every due topic), depth (override). Output: {swept:[{topic,new,queries}]}.")
async def cap_sweep(topic_id: str = "", depth: str = "", trace_id=None):
    st = store()
    topics = [st.get_topic(topic_id)] if topic_id else [t for t in st.topics("active") if core.is_due(t)]
    topics = [t for t in topics if t]
    if topic_id and not topics:
        return {"error": f"unknown topic: {topic_id}"}
    out = []
    for t in topics[: int(config().get("max_topics_per_night", 6)) if not topic_id else 1]:
        out.append(await sweep_topic(t, depth=depth))
    return {"swept": out, "message": f"{sum(x['new'] for x in out)} new signals"}


@capability("intel.refresh", http_method="POST", http_path="/intel/refresh", http_tags=["intel"],
            description="The full cycle for one topic: sweep, read and assess the strongest signals, update the "
                        "assessment. Uses the model (background, CPU-preferred). Input: topic_id (str!), "
                        "queue (true = hand it to the idle queue instead of running now). Output: {sweep, enrich, snapshot}.")
async def cap_refresh(topic_id: str = "", queue: bool = False, trace_id=None):
    t = store().get_topic(topic_id)
    if not t:
        return {"error": f"unknown topic: {topic_id}"}
    if queue:
        return await _submit_nightly(only=[topic_id], title=f"intel refresh: {t['name']}")
    sw = await sweep_topic(t)
    t = store().get_topic(topic_id) or t
    en = await enrich_topic(t)
    sn = await snapshot_topic(t)
    return {"sweep": sw, "enrich": en, "snapshot": sn.get("snapshot") or sn, "message": f"{sw['new']} new, {en['enriched']} assessed"}


# ── reports ──────────────────────────────────────────────────────────────────

async def build_report(topic_ids: List[str], *, period_hours: int = 24, title: str = "",
                       only_new: bool = True) -> Dict[str, Any]:
    st = store()
    topics = [st.get_topic(i) for i in topic_ids] if topic_ids else st.topics("active")
    topics = [t for t in topics if t]
    since = _since(period_hours)
    per: Dict[str, Dict[str, Any]] = {}
    for t in topics:
        sigs = st.signals(t["id"], since=since, limit=80, order="score", unreported=only_new)
        sigs = [s for s in sigs if (s.get("score") or 0) >= float(t.get("min_score", 5))]
        snap = st.latest_snapshot(t["id"])
        if snap and core.parse_ts(snap.get("ts")) and core.parse_ts(snap["ts"]) < core.parse_ts(since):
            snap = None                       # an old assessment is not news
        if not sigs and not snap:
            continue
        acts = core.actions_for(t, sigs, snap)
        per[t["id"]] = {"signals": sigs, "snapshot": snap, "actions": acts}
    topics = [t for t in topics if t["id"] in per]
    doc = core.build_report(topics, per, title=title, period_hours=period_hours)
    if config().get("illustrate") and topics:
        imgs = await _call("media.illustrate", subjects=[t["name"] for t in topics[:2]], count=2)
        for i, im in enumerate((imgs.get("images") or [])[:2]):
            per[topics[i % len(topics)]["id"]].setdefault("images", []).append(
                {"url": im.get("url"), "caption": im.get("caption", "")})
        doc = core.build_report(topics, per, title=title, period_hours=period_hours)
    for tid, d in per.items():
        for a in d["actions"]:
            st.put_action(dict(a, report_id=doc["id"]), report_id=doc["id"])
    html = core.render_html(doc, base_url="")
    st.put_report(doc, html)
    st.mark_reported(s["id"] for d in per.values() for s in d["signals"])
    await _event("report.new", id=doc["id"], title=doc["title"], signals=doc["counts"]["signals"])
    return doc


@capability("intel.report.build", http_method="POST", http_path="/intel/report/build", http_tags=["intel"],
            description="Build an intelligence report now from what is already gathered (no new collection). "
                        "Input: topic_ids (list/csv, blank = all active), period_hours (default 24), title, "
                        "include_reported (re-use signals already in an earlier report), deliver (channels to send "
                        "it to, e.g. telegram,chat), target (session id / email for channels that need one). "
                        "Output: {id, url, title, counts, delivered}.")
async def cap_report_build(topic_ids: Any = None, period_hours: int = 24, title: str = "",
                           include_reported: bool = False, deliver: Any = None, target: str = "", trace_id=None):
    doc = await build_report(_list_arg(topic_ids), period_hours=int(period_hours or 24), title=title,
                             only_new=not include_reported)
    out = {"id": doc["id"], "url": _report_url(doc["id"]), "title": doc["title"], "counts": doc["counts"],
           "empty": not doc["sections"]}
    chans = _list_arg(deliver)
    if chans and doc["sections"]:
        out["delivered"] = (await deliver_report(doc["id"], chans, target=target))["results"]
    return out


@capability("intel.report.list", http_method="GET", http_path="/intel/report/list", http_tags=["intel"],
            memory="off", silent=True,
            description="Recent intelligence reports. Input: topic_id, limit. Output: {rows:[{id,ts,title,url,counts}]}.")
async def cap_report_list(topic_id: str = "", limit: int = 30, trace_id=None):
    rows = store().reports(topic_id=topic_id, limit=int(limit))
    for r in rows:
        r["url"] = _report_url(r["id"])
        r["name"] = r["title"]
        r["meta"] = str(r.get("ts", ""))[:16].replace("T", " ")
    return {"rows": rows, "count": len(rows)}


@capability("intel.report.get", http_method="GET", http_path="/intel/report/get", http_tags=["intel"],
            memory="off", silent=True,
            description="One report's document model (sections) and its delivery log. Input: id. "
                        "Output: {report, deliveries, url, markdown}.")
async def cap_report_get(id: str = "", trace_id=None):
    d = store().get_report(id)
    if not d:
        return {"error": f"unknown report: {id}"}
    return {"report": d, "deliveries": store().deliveries(report_id=id), "url": _report_url(id),
            "markdown": core.render_markdown(d, report_url=_report_url(id, public=True))}


@APP.get("/intel/report/{rid}", include_in_schema=False)
async def _serve_report(rid: str):
    d = store().get_report(re.sub(r"[^a-f0-9]", "", rid), with_html=True)
    if not d:
        return HTMLResponse("<h1>No such report</h1>", status_code=404)
    return HTMLResponse(d.get("_html") or core.render_html(d), headers={"Cache-Control": "no-cache"})


# ── delivery ─────────────────────────────────────────────────────────────────

async def deliver_report(rid: str, channels: List[str], *, target: str = "") -> Dict[str, Any]:
    st = store()
    doc = st.get_report(rid, with_html=True)
    if not doc:
        return {"error": f"unknown report: {rid}", "results": []}
    html_doc = doc.pop("_html", "")
    public = _report_url(rid, public=True)
    action_url = config().get("public_base", "").rstrip("/") + "/intel/act"
    results = []
    for ch in channels:
        spec = core.CHANNELS.get(ch)
        if not spec:
            results.append({"channel": ch, "ok": False, "error": "unknown channel"})
            continue
        if ch == "html":
            results.append({"channel": ch, "ok": True, "url": _report_url(rid)})
            continue
        if ch == "fabric":
            fab = sys.modules.get("data_fabric")
            ok = False
            if fab and getattr(fab, "ingest_dataset", None):
                try:
                    await fab.ingest_dataset(dataset_id="intel.reports", data=[{
                        "id": rid, "title": doc["title"], "ts": doc["ts"], "url": public,
                        "text": core.render_markdown(doc, report_url=public)[:20000]}],
                        source="intel", tags=["intel", "report"], source_id="intel", defer_embedding=True)
                    ok = True
                except Exception as ex:  # noqa: BLE001
                    log.info("intel: report to fabric failed: %s", ex)
            results.append({"channel": ch, "ok": ok})
            continue
        args = core.channel_args(ch, doc, report_url=public, action_url=action_url,
                                 html_doc=html_doc if ch == "email" else "", target=target)
        if args is None:
            results.append({"channel": ch, "ok": False, "error": f"{ch} needs a target ({spec.get('needs_target')})"})
            continue
        if ch == "canvas":
            args["content"]["src"] = _report_url(rid)   # the canvas is same-origin
        cap = spec["cap"]
        if ch == "telegram" and not _has(cap):
            cap, args = "tg.notify", {"text": core.render_markdown(doc, report_url=public)[:3800]}
        r = await _call(cap, **args)
        ok = not r.get("error") and r.get("ok", True) is not False
        results.append({"channel": ch, "ok": ok, "error": r.get("error", "") if not ok else ""})
    for r in results:
        st.log_delivery(rid, r["channel"], core.now_iso(), r["ok"], r)
    st.update_report(rid, delivered=sorted({r["channel"] for r in results if r["ok"]} |
                                           set((st.get_report(rid) or {}).get("delivered") or [])))
    await _event("report.delivered", id=rid, channels=[r["channel"] for r in results if r["ok"]])
    return {"results": results}


@capability("intel.deliver", http_method="POST", http_path="/intel/deliver", http_tags=["intel"],
            description="Send a built report to channels. Input: report_id (str!), channels (list/csv: html, "
                        "telegram, chat, email, canvas, notebook, memory, fabric, podcast, print), target "
                        "(session id for chat/canvas, address for email). Output: {results:[{channel, ok, error}]}.")
async def cap_deliver(report_id: str = "", channels: Any = None, target: str = "", trace_id=None):
    chans = _list_arg(channels) or list(config().get("brief_channels") or ["html"])
    return await deliver_report(report_id, chans, target=target)


@capability("intel.channel.list", http_method="GET", http_path="/intel/channel/list", http_tags=["intel"],
            memory="off", silent=True,
            description="Where intelligence reports can be delivered, and whether each is available now. "
                        "Output: {rows:[{id,label,render,needs_target,available}]}.")
async def cap_channel_list(trace_id=None):
    rows = []
    for cid, c in core.CHANNELS.items():
        avail = not c.get("cap") or _has(c["cap"]) or (cid == "telegram" and _has("tg.notify"))
        rows.append({"id": cid, "name": c["label"], "label": c["label"], "render": c["render"],
                     "needs_target": c.get("needs_target", ""), "available": avail})
    return {"rows": rows}


# ── actions: what a report's buttons do ──────────────────────────────────────

@capability("intel.action.list", http_method="GET", http_path="/intel/action/list", http_tags=["intel"],
            memory="off", silent=True,
            description="Actions reports have offered (get repo, research further, track, read) and what became "
                        "of them. Input: status (offered|running|done|failed). Output: {rows}.")
async def cap_action_list(status: str = "", limit: int = 60, trace_id=None):
    rows = store().actions(status=status, limit=int(limit))
    for r in rows:
        if r.get("status") == "offered" and not core.action_usable(r)[0]:
            r["status"] = "expired"
        r["name"] = r.get("label")
        r["meta"] = r.get("status")
    return {"rows": rows, "count": len(rows)}


@capability("intel.action.run", http_method="POST", http_path="/intel/action/run", http_tags=["intel"],
            description="Run an action a report offered, by its one-time token. The capability and its arguments "
                        "come from the stored action, never from the caller; each token runs once. Input: token "
                        "(str!). Output: {ok, message, result}.")
async def cap_action_run(token: str = "", trace_id=None):
    st = store()
    a = st.get_action(token)
    ok, why = core.action_usable(a)
    if not ok:
        return {"error": why}
    if not st.claim_action(token):
        return {"error": "already started"}
    args = core.action_args(a)
    await _event("action.started", kind=a["kind"], label=a["label"], topic=a.get("topic_id"))
    r = await _call(a["cap"], **args)
    good = not r.get("error")
    st.finish_action(token, "done" if good else "failed", {k: r.get(k) for k in list(r)[:12]})
    msg = {"research": "research started" + (f" (job {r.get('job_id')})" if r.get("job_id") else ""),
           "repo": f"fetched to {r.get('path')}" if r.get("path") else "repo fetched",
           "track": "now tracked", "discover": "mapping started", "read": "read", "sweep": "swept"}.get(a["kind"], "done")
    await _event("action.finished", kind=a["kind"], label=a["label"], ok=good)
    return {"ok": good, "message": msg if good else r.get("error"), "result": r}


@APP.get("/intel/act", include_in_schema=False)
async def _act_page(token: str = ""):
    """A Telegram button lands here. Opening a link must never act by itself
    (link previews and retries fetch it), so this page asks for one press."""
    a = store().get_action(re.sub(r"[^A-Za-z0-9_-]", "", token))
    ok, why = core.action_usable(a)
    e = core._E
    if not a:
        body = "<p>This action is unknown.</p>"
    elif not ok:
        body = f"<p><b>{e(a.get('label', ''))}</b></p><p>{e(why)}.</p>"
    else:
        body = (f"<p><b>{e(a['label'])}</b></p><p class=meta>{e(a.get('query') or a.get('target') or '')}</p>"
                f'<button class="act" data-token="{e(a["token"])}">{e(a.get("glyph", ""))} Go<span class="r"></span></button>')
    page = ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>Vera action</title><style>{core.REPORT_CSS}</style></head><body><main><h1>Vera</h1>{body}</main>"
            + core._ACTION_JS + "</body></html>")
    return HTMLResponse(page)


_REPO_ROOT_ENV = "VERA_INTEL_REPO_DIR"


@capability("intel.repo.get", http_method="POST", http_path="/intel/repo/get", http_tags=["intel"],
            description="Fetch a code repository a report found: a shallow clone (no submodules, no hooks run) into "
                        "the intel repo area, then its README as a digest. Input: url (github/gitlab/codeberg/"
                        "bitbucket repo page or .git url), topic_id. Output: {ok, path, readme, files}.")
async def cap_repo_get(url: str = "", topic_id: str = "", trace_id=None):
    r = core.repo_of(url)
    if not r:
        return {"error": "not a recognised repository url"}
    from Vera.vera.state_paths import state_dir
    root = Path(os.environ.get(_REPO_ROOT_ENV) or state_dir("intel", "repos"))
    dest = root / r["host"] / r["owner"] / r["repo"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="true")
    if (dest / ".git").exists():
        cmd = ["git", "-C", str(dest), "pull", "--ff-only", "--depth", "1"]
    else:
        cmd = ["git", "clone", "--depth", "1", "--no-recurse-submodules", "--single-branch",
               "-c", "core.hooksPath=/dev/null", r["clone"], str(dest)]
    try:
        p = await asyncio.create_subprocess_exec(*cmd, env=env, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(p.communicate(), timeout=300)
    except asyncio.TimeoutError:
        return {"error": "clone timed out after 300s"}
    except FileNotFoundError:
        return {"error": "git is not installed here"}
    if p.returncode != 0:
        return {"error": (err or b"").decode("utf-8", "replace")[-400:]}
    readme = ""
    for nm in ("README.md", "readme.md", "README.rst", "README.txt", "README"):
        f = dest / nm
        if f.exists():
            readme = f.read_text("utf-8", "replace")[:6000]
            break
    files = sorted(x.name for x in dest.iterdir() if not x.name.startswith("."))[:60]
    if topic_id and store().get_topic(topic_id):
        s = core.normalise_signal(topic_id, {"url": r["url"], "title": f"Repo fetched: {r['owner']}/{r['repo']}",
                                             "snippet": readme[:600], "kind": "repo"}, source="action")
        if s:
            s.update(score=6.0, local_path=str(dest))
            store().put_signal(s)
    await _event("repo.fetched", repo=f"{r['owner']}/{r['repo']}", path=str(dest))
    return {"ok": True, "path": str(dest), "readme": readme, "files": files, "repo": r}


# ── dream / narrator / director ──────────────────────────────────────────────

@capability("intel.brief", http_method="GET", http_path="/intel/brief", http_tags=["intel"],
            memory="off", silent=True,
            description="A compact brief of what intelligence is following - each active topic's latest headline, "
                        "new signals and open questions. Read by the dream director and the narrator. Input: limit. "
                        "Output: {text, topics:[{id,name,headline,new_24h,questions}]}.")
async def cap_brief(limit: int = 8, trace_id=None):
    out, lines = [], []
    for t in store().topics("active")[: int(limit)]:
        stt = _topic_stats(t)
        row = {"id": t["id"], "name": t["name"], "headline": stt["headline"], "new_24h": stt["signals_24h"],
               "questions": (t.get("questions") or [])[:3], "depth": t.get("depth")}
        out.append(row)
        lines.append(f"- {t['name']} ({stt['signals_24h']} new): {stt['headline'] or 'no assessment yet'}"
                     + (f" | open: {'; '.join(row['questions'])}" if row["questions"] else ""))
    return {"text": "\n".join(lines) or "no topics tracked", "topics": out}


@capability("intel.ingest_dream", http_method="POST", http_path="/intel/ingest_dream", http_tags=["intel"],
            description="Delivery channel for dreams: a dream's report becomes a signal on an intelligence topic. "
                        "Input: text (str!), target (topic id; blank = the topic whose words it matches best). "
                        "Output: {ok, topic, signal}.")
async def cap_ingest_dream(text: str = "", target: str = "", trace_id=None):
    if not (text or "").strip():
        return {"error": "text required"}
    st = store()
    t = st.get_topic(target) if target else None
    if not t:
        best, score = None, 0
        low = text.lower()
        for cand in st.topics("active"):
            sc = sum(1 for k in core.keywords(cand) if k in low)
            if sc > score:
                best, score = cand, sc
        t = best if score >= 2 else None
    if not t:
        return {"ok": False, "error": "no topic matches this dream output"}
    first = next((ln.strip("# ").strip() for ln in text.splitlines() if ln.strip()), "Dream finding")
    s = core.normalise_signal(t["id"], {"title": f"Dream: {first[:120]}", "snippet": text[:1200], "kind": "dream"},
                              source="dream")
    if not s:
        return {"ok": False, "error": "unusable"}
    s["score"] = 6.5
    s["summary"] = core.clean_prose(text)[:1500]
    st.put_signal(s)
    await _event("signal.new", topic=t["id"], id=s["id"], title=s["title"], source="dream")
    return {"ok": True, "topic": t["id"], "signal": s["id"]}


def _wire_dream() -> None:
    """Register the `intel` delivery channel (dream -> intel) and a narrator probe.
    Both are optional seams: absent subsystems are simply skipped."""
    try:
        from Vera.vera import delivery as _delivery
        _delivery.register_channel("intel", label="Intelligence topic", cap="intel.ingest_dream",
                                   needs_target=False, target_field="target",
                                   target_label="Topic id (blank = best match)", source="builtin")
    except Exception as ex:  # noqa: BLE001
        log.info("intel: delivery channel not registered: %s", ex)
    dc = sys.modules.get("dream_capabilities")
    probes = getattr(dc, "_NARRATOR_PROBES", None) if dc else None
    if isinstance(probes, dict):
        probes.setdefault("intel.brief", {"desc": "topics intelligence follows: headline, new signals, open questions",
                                          "args": "{limit:int}"})


_wire_dream()


# ── nightly refresh (idle queue) and the morning brief ───────────────────────

async def _intel_job(job: Dict[str, Any], should_continue) -> Dict[str, Any]:
    """The nightly refresh: due topics (or the ones named), sweep -> assess ->
    snapshot, stopping between steps the moment the box is wanted. Ends by
    building the brief, which the morning tick only delivers."""
    st = store()
    payload = job.get("payload") or {}
    only = payload.get("only") or []
    cap = int(config().get("max_topics_per_night", 6))
    topics = [st.get_topic(i) for i in only] if only else [t for t in st.topics("active") if core.is_due(t)][:cap]
    topics = [t for t in topics if t]
    done = []
    for t in topics:
        why = await _stop_reason(should_continue)
        if why:
            return {"yielded": why, "done": done}
        await sweep_topic(t)
        if await _stop_reason(should_continue):
            return {"yielded": "pre-empted", "done": done}
        t = st.get_topic(t["id"]) or t
        await enrich_topic(t, should_continue=should_continue)
        if await _stop_reason(should_continue):
            return {"yielded": "pre-empted", "done": done}
        await snapshot_topic(st.get_topic(t["id"]) or t)
        done.append(t["id"])
    res: Dict[str, Any] = {"ok": True, "topics": done}
    if not only and done:
        doc = await build_report([], period_hours=int(config().get("report_period_hours", 24)))
        if doc["sections"]:
            st.set_kv("pending_brief", {"id": doc["id"], "ts": doc["ts"]})
            res["report"] = doc["id"]
    st.set_kv("last_nightly", {"ts": core.now_iso(), "topics": done})
    return res


async def _submit_nightly(*, only: Optional[List[str]] = None, title: str = "intel nightly refresh") -> Dict[str, Any]:
    try:
        from Vera.vera import idle_queue_service as _svc
    except Exception:  # noqa: BLE001
        _svc = None
    if _svc is None:
        asyncio.create_task(_intel_job({"payload": {"only": only or []}}, None))
        return {"ok": True, "queued": False, "message": "idle queue unavailable - running now"}
    r = await _svc.submit(KIND_INTEL, title, dedupe_key="intel:" + (",".join(only) if only else "nightly"),
                          payload={"only": only or []}, total=len(only or []) or None)
    return {"ok": True, "queued": True, "job": r, "message": "queued for the next quiet window"}


try:
    from Vera.vera import idle_queue_service as _svc_reg
    _svc_reg.register_handler(KIND_INTEL, _intel_job)
except Exception as _ex:  # noqa: BLE001
    log.info("intel: idle queue handler not registered: %s", _ex)


def _local_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(config().get("timezone") or "Europe/London"))
    except Exception:  # noqa: BLE001
        return datetime.now()


async def _tick() -> None:
    """Every few minutes: queue the nightly refresh once a night from sweep_hour;
    deliver the brief once a day from brief_hour. Delivery never generates - if
    the night produced no report, the brief is assembled from what exists."""
    c = config()
    if not c.get("enabled"):
        return
    st = store()
    now = _local_now()
    today = now.date().isoformat()
    if now.hour >= int(c.get("sweep_hour", 1)) and now.hour < int(c.get("brief_hour", 7)) \
            and st.get_kv("nightly_day") != today and st.topics("active"):
        st.set_kv("nightly_day", today)
        await _submit_nightly()
    bh = int(c.get("brief_hour", 7))
    if bh <= now.hour < bh + 4 and st.get_kv("brief_day") != today:
        st.set_kv("brief_day", today)
        pend = st.get_kv("pending_brief") or {}
        rid = pend.get("id")
        if not rid:
            doc = await build_report([], period_hours=int(c.get("report_period_hours", 24)))
            rid = doc["id"] if doc["sections"] else ""
        if rid:
            await deliver_report(rid, list(c.get("brief_channels") or ["html"]))
        st.set_kv("pending_brief", {})


schedule(_tick, 300, name="intel.tick", skip_in_sandbox=True, singleton=True)


@capability("intel.config.get", http_method="GET", http_path="/intel/config", http_tags=["intel"],
            memory="off", silent=True,
            description="Intelligence settings: nightly refresh hour, brief hour and channels, topics per night, "
                        "public link base. Output: {config, schedule}.")
async def cap_config_get(trace_id=None):
    st = store()
    return {"config": config(), "schedule": {"nightly_day": st.get_kv("nightly_day"),
                                             "brief_day": st.get_kv("brief_day"),
                                             "pending_brief": st.get_kv("pending_brief"),
                                             "last_nightly": st.get_kv("last_nightly")}}


@capability("intel.config.set", http_method="POST", http_path="/intel/config", http_tags=["intel"],
            description="Change intelligence settings. Input: any of enabled, timezone, sweep_hour, brief_hour, "
                        "max_topics_per_night, report_period_hours, brief_channels (list/csv), public_base, "
                        "illustrate. Output: {ok, config}.")
async def cap_config_set(enabled: bool = None, timezone: str = None, sweep_hour: int = None,
                         brief_hour: int = None, max_topics_per_night: int = None, report_period_hours: int = None,
                         brief_channels: Any = None, public_base: str = None, illustrate: bool = None, trace_id=None):
    cur = store().get_kv("config", {}) or {}
    for k, v in (("enabled", enabled), ("timezone", timezone), ("sweep_hour", sweep_hour),
                 ("brief_hour", brief_hour), ("max_topics_per_night", max_topics_per_night),
                 ("report_period_hours", report_period_hours), ("public_base", public_base),
                 ("illustrate", illustrate)):
        if v is not None:
            cur[k] = v
    if brief_channels is not None:
        cur["brief_channels"] = [c for c in _list_arg(brief_channels) if c in core.CHANNELS]
    for k in ("sweep_hour", "brief_hour"):
        if k in cur:
            cur[k] = max(0, min(23, int(cur[k])))
    store().set_kv("config", cur)
    return {"ok": True, "config": config()}


# ── leaving n8n ──────────────────────────────────────────────────────────────

async def _browse_all(dataset_id: str, cap: int = 4000) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    off = 0
    while off < cap:
        r = await _call("fabric.browse", dataset_id=dataset_id, limit=200, offset=off)
        recs = r.get("records") or []
        out += [x.get("data") or {} for x in recs]
        if not r.get("has_more") or not recs:
            break
        off += len(recs)
    return out


def _latest_by(rows: List[Dict[str, Any]], key: str, ts: str) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        k = str(r.get(key) or "")
        if k and (k not in out or str(r.get(ts) or "") >= str(out[k].get(ts) or "")):
            out[k] = r
    return out


@capability("intel.import.n8n", http_method="POST", http_path="/intel/import/n8n", http_tags=["intel"],
            description="Bring the n8n intelligence state into Vera: topics from vera.interests and intel.sources "
                        "(grouped by their topic), and the enriched items from intel.enrichments as assessed "
                        "signals. Idempotent. Input: dry_run (default true - report what would be imported). "
                        "Output: {topics, signals, dry_run}.")
async def cap_import_n8n(dry_run: bool = True, trace_id=None):
    st = store()
    plans: Dict[str, Dict[str, Any]] = {}
    for it in _latest_by(await _browse_all("vera.interests"), "topic", "created").values():
        if it.get("active") is False or not it.get("topic"):
            continue
        tid = core.slug(it["topic"])
        plans[tid] = {"name": it["topic"], "brief": it.get("note", ""), "queries": [], "origin": "n8n:vera.interests"}
    groups: Dict[str, List[str]] = {}
    for src in _latest_by(await _browse_all("intel.sources"), "id", "added").values():
        if not src.get("query") or src.get("enabled") is False:
            continue
        groups.setdefault(str(src.get("topic") or "intel"), []).append(src["query"])
    _NAMES = {"ai_ml": "AI and machine learning", "notes": "From my notes", "property": "Property market",
              "proptech": "PropTech", "competitors": "Competitors"}
    for grp, qs in groups.items():
        name = _NAMES.get(grp, grp.replace("_", " ").title())
        tid = core.slug(name)
        p = plans.setdefault(tid, {"name": name, "brief": "", "queries": [], "origin": f"n8n:intel.sources:{grp}"})
        p["queries"] = list(dict.fromkeys(p["queries"] + qs))[:24]
        p["n8n_group"] = grp
    sigs = []
    for en in _latest_by(await _browse_all("intel.enrichments"), "url", "enriched_at").values():
        grp = str(en.get("topic") or "")
        tid = next((k for k, p in plans.items() if p.get("n8n_group") == grp), None)
        if not tid:
            continue
        s = core.normalise_signal(tid, {"url": en.get("url"), "title": en.get("title"),
                                        "snippet": en.get("summary"), "seen": en.get("enriched_at")}, source="n8n")
        if not s:
            continue
        s.update(summary=en.get("summary", ""), so_what=en.get("so_what", ""), tags=en.get("tags") or [],
                 entities=en.get("entities") or [], enriched=True, enriched_at=en.get("enriched_at"),
                 score=float(en["relevance"]) if isinstance(en.get("relevance"), (int, float)) else None,
                 reported=True)
        sigs.append(s)
    if not dry_run:
        for tid, p in plans.items():
            existing = st.get_topic(tid)
            st.put_topic(core.normalise_topic({**p, "id": tid}, existing))
        for s in sigs:
            if not st.has_signal(s["id"]):
                st.put_signal(s)
        st.set_kv("n8n_imported", core.now_iso())
    return {"dry_run": dry_run, "topics": [{"id": k, "name": v["name"], "queries": len(v["queries"])}
                                           for k, v in plans.items()], "signals": len(sigs)}


_N8N_FLOWS = re.compile(r"^(intel-|vera-ingest-interests$|vera-daily-report$|vera-report-now$|vera-interest$|"
                        r"vera-tag$|vera-deep-monitor$)")


@capability("intel.n8n.retire", http_method="POST", http_path="/intel/n8n/retire", http_tags=["intel"],
            description="Switch off the n8n intelligence workflows Vera now replaces (intel-*, vera-ingest-interests, "
                        "vera-daily-report, vera-deep-monitor and their webhooks). Deactivates only - never deletes. "
                        "Input: dry_run (default true). Output: {workflows:[{id,name,active,action}]}.")
async def cap_n8n_retire(dry_run: bool = True, trace_id=None):
    r = await _call("n8n.workflow.list", limit=250)
    if r.get("error"):
        return r
    rows = []
    for w in r.get("workflows") or r.get("data") or r.get("rows") or []:
        name = str(w.get("name") or "")
        if not _N8N_FLOWS.match(name):
            continue
        act = "would deactivate" if w.get("active") else "already inactive"
        if not dry_run and w.get("active"):
            x = await _call("n8n.workflow.activate", id=str(w.get("id")), active=False)
            act = "deactivated" if not x.get("error") else f"failed: {x['error']}"
        rows.append({"id": w.get("id"), "name": name, "active": w.get("active"), "action": act})
    if not dry_run:
        store().set_kv("n8n_retired", core.now_iso())
    return {"dry_run": dry_run, "workflows": rows}


# ── UI ───────────────────────────────────────────────────────────────────────

@capability("intel.panel.html", http_method="GET", http_path="/intel/panel", http_tags=["intel"],
            memory="off", silent=True, description="The Intelligence panel page.")
async def cap_panel_html(trace_id=None):
    return HTMLResponse(_PANEL.read_text(encoding="utf-8") if _PANEL.exists() else "<h1>intel panel missing</h1>",
                        headers={"Cache-Control": "no-cache"})


@APP.get("/ui/elements/intel_timeline.js", include_in_schema=False)
async def _serve_timeline_js():
    if _TIMELINE_JS.exists():
        return Response(content=_TIMELINE_JS.read_text(encoding="utf-8"), media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})
    return Response(content="console.warn('intel timeline element missing');", media_type="application/javascript")


register_ui(
    "intel", "Intelligence", "◎",
    html='<div style="height:100%;display:flex;flex-direction:column"><iframe src="/intel/panel" '
         'style="flex:1;border:none;width:100%;height:100%"></iframe></div>',
    ui_caps=["intel.topic.list", "intel.topic.save", "intel.signal.list", "intel.timeline.get",
             "intel.report.build", "intel.report.list", "intel.deliver", "intel.refresh"],
    mode="tab", tab_order=58,
)
