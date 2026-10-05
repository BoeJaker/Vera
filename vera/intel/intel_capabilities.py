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

Triage is done by the NLP tier (embeddings for novelty and story joining, a
reranker for relevance, GLiNER for entities, extractive QA for open questions,
sentiment); a model only writes summaries for what passes the gate, and the
35B-A3B MoE writes the few long things (assessments, the brief, proposals)
under the `intel` routing profile.

Background work goes through the idle queue one unit at a time (a topic, the
drop folders, the advisor, a research question) whenever the box is quiet - the
nightly batch, due topics during the day, and planned work inside sessions -
as `job_type: idle_intel`, CPU-preferred and demoted when a person appears. The
morning brief is built at night and only delivered in the morning.

Woven into: fabric discovery (sweeps), the dream director (a briefing section
and an `intel` delivery channel that turns dream output into signals), the
narrator (a probe), the canvas and widgets (readable caps, a layout, an
element), and places (`intel`).

Replaces the n8n intelligence flows (intel-night/brief/automations/command,
files-night, the vera-* interest/monitor/report/podcast flows): `intel.import.n8n`
brings their state over; `intel.n8n.retire` switches them off (dry run first).
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Request
from fastapi.responses import HTMLResponse, Response

import Vera.vera.capability_orchestration as _orch
from Vera.vera.capability_orchestration import (
    APP, CAPABILITY_REGISTRY, capability, emit_event, enum_schema, register_ui, schedule,
)
try:
    from Vera.vera.intel import intel_core as core
    from Vera.vera.intel import intel_ops_core as ops
    from Vera.vera.intel import intel_ledger_core as ledger
    from Vera.vera.intel import intel_change_core as chg
    from Vera.vera.intel import intel_pm_core as pm
    from Vera.vera.intel import intel_geo_core as geo
    from Vera.vera.intel.intel_store import IntelStore
except ImportError:                       # loaded from its file under a stand-in orchestration (tests)
    from vera.intel import intel_core as core
    from vera.intel import intel_ops_core as ops
    from vera.intel import intel_ledger_core as ledger
    from vera.intel import intel_change_core as chg
    from vera.intel import intel_pm_core as pm
    from vera.intel import intel_geo_core as geo
    from vera.intel.intel_store import IntelStore

log = logging.getLogger("vera.intel")

_HERE = Path(__file__).parent
_PANEL = _HERE / "intel_panel.html"
_TIMELINE_JS = _HERE / "intel_timeline_element.js"

KIND_INTEL = "intel"
SIGNALS_DATASET = "intel.signals"

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,              # the nightly refresh + morning brief
    "timezone": "Europe/London",
    "sweep_hour": 23,             # the nightly batch is queued from this hour (n8n: intel-night 23:00)
    "brief_hour": 7,              # the brief is delivered from this hour (n8n: intel-brief 06:30, daily report 07:00)
    "max_topics_per_night": 12,
    "report_period_hours": 24,
    "brief_channels": ["html", "telegram", "phone", "print", "podcast"],   # the n8n fan-out
    "alert_channels": ["telegram"],
    "public_base": "https://llm.int:8999",   # links in Telegram / phone / email must work off the box
    "illustrate": False,
    "persona": ops.DEFAULT_PERSONA,
    "competitors": [],
    "phone_target": "mobile_app_smasnug",
    "printer_id": "prn_d332ecadad",
    "files_host": "dadeb47a-00a7-4470-85d4-61977ef7b516",   # VFS-02 over the ssh host store
    "drop_folders": [
        {"kind": "notes", "path": "/srv/pools/tank_sde/vfs/home/notes"},
        {"kind": "inbox", "path": "/srv/pools/tank_sde/vfs/home/inbox"},
        {"kind": "print", "path": "/srv/pools/tank_sde/vfs/home/print"},
        {"kind": "podcast", "path": "/srv/pools/tank_sde/vfs/home/podcast"},
    ],
    "files_model_budget": 8,      # model calls per night for notes + inbox
    "weekly_day": 0, "weekly_hour": 8,          # Monday 08:00 week in review
    "advise_day": 6, "advise_hour": 3,          # Sunday 03:00 automation proposals
    "night_summary": True,
    "nlp": True,                  # use the NLP tier (embed / rerank / GLiNER / QA / sentiment)
    "llm_gate": 5.0,              # a signal needs this score before a model reads it
    "opportunistic": True,        # refresh due topics whenever the box is idle, not only at night
    "daily_job_budget": 24,       # topic jobs per day outside the nightly batch and sessions
    "sessions": [],               # [{name, days:[0-6], start:"HH:MM", end:"HH:MM", budget, mode}]
    "alerts": True,               # push a material change in an assessment
    "auto_research": True,        # deep topics: research new questions inside sessions
}

_CONFIG_KEYS = set(DEFAULT_CONFIG)

_STORE: Optional[IntelStore] = None
_OWN = {"llm_ended": 0.0}
_PROFILE = {"declared": False}


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


async def _call(name: str, /, **kw) -> Dict[str, Any]:
    """Call a capability in-process, dropping arguments it does not take; a missing
    capability or a raise comes back as {error} instead of breaking the caller.
    `name` is positional-only: some capabilities take a `name` argument of their own."""
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


def _declare_profile() -> None:
    """The `intel` routing profile: every intel role on the 35B-A3B MoE the director
    already keeps warm on its CPU node (≈3B active parameters, so it is the fastest
    capable model on CPU as well as the best). Overridable per role on Model Routing."""
    reg = getattr(_orch, "register_routing_profile", None)
    if not reg:
        return
    model = getattr(_orch, "LONG_HORIZON_CPU_MODEL", "qwen3.6:35b-a3b")
    node = getattr(_orch, "LONG_HORIZON_CPU_NODE", "cpu-247")
    opts = dict(getattr(_orch, "LONG_HORIZON_CPU_OPTIONS", {"num_ctx": 16384, "keep_alive": "2h"}))
    long = {"job_type": "idle_intel", "deny_gpu": True, "pin": node, "model": model, "options": opts}
    try:
        reg("intel", label="Intelligence", owner="intel", roles={
            # measured on prod 2026-10-04: the default 9B dense model on gpu-250-cpu ran a source summary at
            # 0.8 tok/s (282 s); the MoE (≈3B active) on cpu-247 did the same prompt in 33.5 s, and better
            "enrich": dict(long, label="intel · read a source (MoE)"),
            "notes": dict(long, label="intel · notes and inbox (MoE)"),
            "assess": dict(long, label="intel · topic assessment (MoE)"),
            "brief": dict(long, label="intel · the brief's top line (MoE)"),
            "advise": dict(long, label="intel · automation advisor (MoE)"),
        })
        _PROFILE["declared"] = True
    except Exception as ex:  # noqa: BLE001
        log.info("intel: routing profile not declared: %s", ex)


_declare_profile()


async def _llm_json(prompt: str, *, role: str = "enrich", timeout: float = 900.0) -> Optional[Dict[str, Any]]:
    """One background model call parsed as JSON: the analyst persona as system
    prompt, never thinking aloud, CPU-preferred, marked as background work so it
    is demoted the moment a person needs the machine."""
    gen = getattr(_orch, "ollama_generate", None)
    if gen is None:
        return None
    kw: Dict[str, Any] = dict(json_mode=True, think=False, prefer_gpu=False, job_type="idle_intel",
                              timeout=timeout, system=str(config().get("persona") or ops.DEFAULT_PERSONA))
    if _PROFILE["declared"]:
        kw.update(profile="intel", role=role)
    bg = getattr(_orch, "BACKGROUND_LLM", None)
    tok = None
    t0 = time.time()
    try:
        if bg is not None:
            tok = bg.set(f"intel:{role}")
        try:
            raw = await gen(prompt, **kw)
        except TypeError:
            kw.pop("profile", None)
            kw.pop("role", None)
            raw = await gen(prompt, **kw)
    except Exception as ex:  # noqa: BLE001
        log.warning("intel: model call failed: %s", ex)
        return None
    finally:
        if tok is not None:
            try:
                bg.reset(tok)
            except (ValueError, LookupError):
                pass
        _OWN["llm_ended"] = time.time()
        _spend(f"model.{role}", time.time() - t0)
    p = core.parse_json_loose(raw or "")
    return p if isinstance(p, dict) else None


async def _nlp(name: str, **kw) -> Dict[str, Any]:
    """An NLP-tier call (node-served, no GPU); {} when switched off or unavailable,
    so every caller has a heuristic fallback."""
    if not config().get("nlp", True) or not _has(name):
        return {}
    t0 = time.time()
    r = await _call(name, **kw)
    _spend(name, time.time() - t0)
    return {} if r.get("error") else r


def _spend(key: str, seconds: float) -> None:
    """Calls and seconds per model role / NLP task per day (the spend meter; four-minds
    reads intel's real cpu-247 minutes from it)."""
    try:
        st = store()
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rec = st.get_kv(f"spend:{day}", {}) or {}
        e = rec.setdefault(key, {"calls": 0, "seconds": 0.0})
        e["calls"] += 1
        e["seconds"] = round(e["seconds"] + float(seconds), 1)
        st.set_kv(f"spend:{day}", rec)
    except Exception:  # noqa: BLE001 - a meter must never break the work it measures
        pass


_LEDGER_TASKS: set = set()


def _to_ledger(row: Dict[str, Any]) -> None:
    """Mirror a Decision/Outcome v0 row into the shared decision ledger
    (decide.record), in the background and best effort: the local store stays
    the record, and intel.decisions.export -> decide.ledger.import backfills."""
    if not _has("decide.record"):
        return
    try:
        task = asyncio.get_running_loop().create_task(_call("decide.record", row=row))
    except RuntimeError:                     # no loop (a sync caller in a test)
        return
    _LEDGER_TASKS.add(task)
    task.add_done_callback(_LEDGER_TASKS.discard)


def _decide(question_id: str, answer: Any, *, state: Any, ref_kind: str, ref_id: str, **kw) -> Optional[str]:
    """Record one decision in the four-minds Decision v0 shape: the local store,
    mirrored to the shared ledger. Never raises; returns the decision id."""
    try:
        d = ledger.make_decision(question_id, answer, state=state, state_ref={"kind": ref_kind, "id": ref_id}, **kw)
        store().put_decision(d)
        _to_ledger(d)
        return d["decision_id"]
    except Exception as ex:  # noqa: BLE001
        log.debug("intel: decision not recorded: %s", ex)
        return None


def _outcome(ref_kind: str, ref_id: str, source: str, *, questions: Optional[List[str]] = None,
             detail: Optional[Dict[str, Any]] = None) -> int:
    """Give every decision taken on one item an outcome from a product event."""
    n = 0
    try:
        st = store()
        for d in st.decisions_for(ref_kind, ref_id):
            if questions and d.get("question_id") not in questions:
                continue
            o = ledger.make_outcome(d, source, detail=detail)
            if o:
                st.put_outcome(o)
                _to_ledger(o)
                n += 1
    except Exception as ex:  # noqa: BLE001
        log.debug("intel: outcome not recorded: %s", ex)
    return n


async def _event(_name: str, **fields: Any) -> None:
    try:
        await emit_event({**fields, "type": f"intel.{_name}"})
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
    return f"{base}/intel/report/view/{rid}"


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
            schema=enum_schema(depth=list(core.DEPTHS), cadence=list(core.CADENCES), status=list(core.STATUSES),
                               kind=[""] + list(ops.TOPIC_KINDS)),
            description="Create or update a tracked topic. Input: name (str!), id (to update), brief (what it is), "
                        "why (why it matters), questions / queries / entities / exclude / sites (lists or lines), "
                        "kind (general|finance|property|osint|tech - which entity types its sources are tagged "
                        "with; blank = worked out from its words), labels (extra entity types), "
                        "depth (scan|standard|deep), cadence, status, deliver (channels), min_score, dream_loop "
                        "(also run a dream think-loop on it), parent. Output: {ok, topic}.")
async def cap_topic_save(name: str = "", id: str = "", brief: str = None, why: str = None,
                         questions: Any = None, queries: Any = None, entities: Any = None,
                         exclude: Any = None, sites: Any = None, depth: str = "", cadence: str = "",
                         status: str = "", deliver: Any = None, min_score: float = None,
                         dream_loop: bool = None, discovery: bool = None, vendors: bool = None, parent: str = "",
                         color: str = "", origin: str = "", kind: str = None, labels: Any = None, trace_id=None):
    st = store()
    existing = st.get_topic(id) if id else (st.get_topic(core.slug(name)) if name else None)
    raw: Dict[str, Any] = {"name": name, "id": id, "depth": depth, "cadence": cadence, "status": status,
                           "parent": parent, "color": color, "origin": origin}
    for k, v in (("brief", brief), ("why", why), ("min_score", min_score), ("dream_loop", dream_loop),
                 ("discovery", discovery), ("vendors", vendors), ("kind", kind)):
        if v is not None:
            raw[k] = v
    for k, v in (("questions", questions), ("queries", queries), ("entities", entities),
                 ("exclude", exclude), ("sites", sites), ("deliver", deliver), ("labels", labels)):
        if v is not None:
            raw[k] = _list_arg(v)
    raw = {k: v for k, v in raw.items() if v not in ("", None) or k in ("brief", "why", "kind")}
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
                       st.reports(topic_id=topic_id, since=since, limit=500), days=days,
                       changes=_changes(topic_id, since=since))
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


# ── collection: sweep (no model), enrich (NLP tier, then a model), snapshot (MoE) ──

async def _triage(t: Dict[str, Any], cands: List[Dict[str, Any]], *, stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Score new candidates with the NLP tier: one embedding batch (novelty and
    story joining against the topic's last 30 days) and one rerank batch
    (relevance to the topic's profile). A candidate that is the same story as one
    already held joins it instead of becoming a new signal."""
    st = store()
    if not cands:
        return []
    texts = [f"{c.get('title', '')}. {c.get('snippet', '')}"[:1000] for c in cands]
    recent = st.signals(t["id"], since=_since(24 * 30), limit=400)
    recent_titles = [s.get("title", "") for s in recent]
    held = [(s["id"], s["vec"]) for s in recent if isinstance(s.get("vec"), list) and s.get("vec")][:200]
    emb = await _nlp("nlp.embed", texts=texts)
    vecs = emb.get("embeddings") if isinstance(emb.get("embeddings"), list) else []
    rr = await _nlp("nlp.rerank", query=ops.profile_text(t), documents=texts, top_k=len(texts))
    rel: Dict[int, float] = {}
    for x in rr.get("ranked") or []:
        try:
            rel[int(x.get("index"))] = ops.squash(x.get("score"))
        except (TypeError, ValueError):
            continue
    out: List[Dict[str, Any]] = []
    batch: Dict[str, Dict[str, Any]] = {}
    genre_budget = 40
    for i, c in enumerate(cands):
        vec = vecs[i] if i < len(vecs) and isinstance(vecs[i], list) else None
        if vec:
            sid, cos = await asyncio.to_thread(ops.nearest, vec, list(held))
            if cos >= 0.92:                 # the same story under another url: it joins, it is not news
                twin = batch.get(sid)
                if twin is not None:        # ...a story that arrived earlier in this same sweep
                    if c.get("url") and c["url"] not in twin.setdefault("also", []):
                        twin["also"] = (twin["also"] + [c["url"]])[-12:]
                else:
                    old = st.get_signal(sid)
                    if old and c.get("url") and c["url"] not in (old.get("also") or []):
                        st.put_signal(dict(old, also=((old.get("also") or []) + [c["url"]])[-12:]))
                stats["joined"] = stats.get("joined", 0) + 1
                continue
            nv = round(1.0 - max(0.0, cos), 3)
            c["vec"] = [round(float(v), 4) for v in vec]
            held.append((c["id"], c["vec"]))
        else:
            nv = core.novelty(c, recent_titles)
            if nv < 0.15:
                continue
        c["novelty"] = nv
        heur = core.heuristic_score(c, t, novelty_v=nv)
        c["relevance_nlp"] = rel.get(i)
        c["score"] = ops.blend_score(relevance=rel.get(i), heuristic=heur, novelty=nv)
        sf = ops.staleness_factor(c, datetime.now(timezone.utc).year)
        if sf < 1.0:
            c["score"], c["stale"] = round(c["score"] * sf, 1), True
        if genre_budget > 0 and (rel.get(i) is None or rel.get(i, 0) >= 0.2):
            genre_budget -= 1         # only plausible hits are classified: a few dozen ~100 ms node calls per sweep
            if ops.index_hint(c.get("url", ""), c.get("title", "")):
                g, gp = "index", 0.9         # a headline hub or section front: no node call needed
            else:
                g, gp = ops.genre_of(await _nlp("nlp.zeroshot", text=texts[i][:600],
                                                labels=list(ops.GENRE_LABELS.values()), multi_label=False))
            if g:
                c["genre"], c["genre_p"] = g, gp
                f = ops.genre_factor(g, bool(t.get("vendors")))
                if f < 1.0:
                    c["score"] = round(c["score"] * f, 1)
                    stats["demoted"] = stats.get("demoted", 0) + 1
        recent_titles.append(c["title"])
        batch[c["id"]] = c
        out.append(c)
        rel_ans = "relevant" if (c.get("score") or 0) >= float(t.get("min_score", 5)) else "not_relevant"
        _decide("intel.signal_relevant", rel_ans, state=ledger.signal_state(c, t["id"]), ref_kind="signal",
                ref_id=c["id"], decider="rule", mode="rule",
                detail={"score": c.get("score"), "rerank": rel.get(i), "novelty": c.get("novelty"),
                        "threshold": float(t.get("min_score", 5))})
        if c.get("genre") or c.get("genre_p") is not None:
            _decide("intel.page_genre", c.get("genre") or "unsure", state=ledger.signal_state(c, t["id"]),
                    ref_kind="signal", ref_id=c["id"], decider="student", mode="rule",
                    detail={"score": c.get("genre_p")})
    stats["nlp"] = bool(vecs or rel)
    return out


async def sweep_topic(t: Dict[str, Any], *, depth: str = "") -> Dict[str, Any]:
    """Run the topic's next searches through discovery-first web search (every
    page also lands in the topic's discovery dataset and entity graph), then let
    the NLP tier score what came back. No model call."""
    st = store()
    budget = core.DEPTH_BUDGET.get(depth or t.get("depth") or "standard", core.DEPTH_BUDGET["standard"])
    queries, cursor = core.rotate_queries(t, budget["queries"])
    errors: List[str] = []
    cands: List[Dict[str, Any]] = []
    ids = set()
    for q in queries:
        r = await _call("web.search", query=q, limit=budget["results"],
                        dataset_id=(f"intel.{t['id']}" if t.get("discovery", True) else ""),
                        discover=("background" if t.get("discovery", True) else "off"))
        if r.get("error"):
            errors.append(f"{q}: {r['error']}")
            continue
        muted = set(t.get("mute_sites") or [])
        for hit in r.get("results") or []:
            s = core.normalise_signal(t["id"], hit, source="web", query=q)
            if s and s.get("site") in muted:
                continue
            if s and s["id"] not in ids and not st.has_signal(s["id"]):
                ids.add(s["id"])
                cands.append(s)
    stats: Dict[str, Any] = {}
    new = await _triage(t, cands, stats=stats)
    for s in new:
        st.put_signal(s)
    new += await _dream_signals(t)
    new += await _research_signals(t)
    st.patch_topic(t["id"], {"query_cursor": cursor, "last_sweep": core.now_iso()})
    await _to_fabric(new)
    for s in new[:20]:
        await _event("signal.new", topic=t["id"], id=s["id"], title=s["title"], score=s.get("score"))
    return {"topic": t["id"], "queries": queries, "new": len(new), "joined": stats.get("joined", 0),
            "demoted": stats.get("demoted", 0), "nlp": stats.get("nlp", False), "errors": errors}


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


async def _research_signals(t: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Research runs started for this topic (sessions, report buttons) that have
    finished since: their result becomes a research signal."""
    st = store()
    jobs = st.get_kv("research_jobs", {}) or {}
    out, keep = [], {}
    for jid, j in jobs.items():
        if j.get("topic_id") != t["id"]:
            keep[jid] = j
            continue
        r = await _call("research.job.status", job_id=jid)
        status = str(r.get("status") or "")
        if status not in ("done", "completed", "complete", "finished"):
            if status in ("failed", "error", "cancelled", "not_found") or (not status and r.get("error")):
                continue                       # gone - forget it
            keep[jid] = j
            continue
        res = r.get("result")
        text = res if isinstance(res, str) else str((res or {}).get("report") or (res or {}).get("text") or res or "")
        s = core.normalise_signal(t["id"], {"title": f"Research: {j.get('query', '')[:120]}", "snippet": text[:1500],
                                            "kind": "research"}, source=f"research:{jid}")
        if s and not st.has_signal(s["id"]):
            s.update(score=7.5, summary=core.clean_prose(text)[:1500], enriched=True)
            st.put_signal(s)
            out.append(s)
    st.set_kv("research_jobs", keep)
    return out


async def _to_fabric(sigs: List[Dict[str, Any]]) -> None:
    """Signals also live in the fabric (one dataset), so search, the worldview and
    discovery see them; embedding is deferred to the backfill, off the hot path."""
    sigs = [s for s in sigs if s]
    if not sigs:
        return
    fab = sys.modules.get("data_fabric")
    ingest = getattr(fab, "ingest_dataset", None) if fab else None
    if not ingest:
        return
    rows = [{k: s.get(k) for k in ("id", "topic_id", "title", "url", "snippet", "summary", "so_what", "kind",
                                   "site", "seen", "score", "tags", "entities", "competitors")} for s in sigs]
    try:
        await ingest(dataset_id=SIGNALS_DATASET, data=rows, source="intel", tags=["intel"],
                     source_id="intel", defer_embedding=True)
    except Exception as ex:  # noqa: BLE001
        log.info("intel: fabric ingest skipped: %s", ex)


async def _stop_reason(probe) -> str:
    """The idle queue's probe is async and answers why to STOP ('' = carry on).
    A 'gate held' answer right after our own model call is our own hold seen by
    the idle tick, not a person - it is ignored rather than yielding to ourselves."""
    if probe is None:
        return ""
    try:
        r = probe()
        if inspect.isawaitable(r):
            r = await r
    except Exception:  # noqa: BLE001
        return ""
    why = str(r or "")
    if why and re.search(r"gate|held", why, re.I):
        human = False
        try:
            human = bool(getattr(_orch, "interactive_recent", lambda: False)())
        except Exception:  # noqa: BLE001
            human = False
        if "intel" in why.lower() and not human:
            return ""                 # held by our own call
        if re.search(r"was held", why, re.I) and not human and time.time() - _OWN["llm_ended"] < 200:
            return ""                 # the cooldown after our own call, with nobody at the keyboard
    return why


def _extractive_summary(text: str, n: int = 2) -> str:
    sents = re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text or "").strip())
    return " ".join(s for s in sents[:n] if len(s) > 20)[:600]


async def _read_and_tag(t: Dict[str, Any], s: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """The NLP-tier pass over one source: reader text, grounded entities (GLiNER),
    extractive answers to the topic's open questions, stance, competitor mentions."""
    page: Dict[str, Any] = {}
    if s.get("url"):
        r = await _call("reader.page", url=s["url"], max_chars=20000)
        if r.get("ok") or r.get("markdown"):
            page = r
    text = str(page.get("markdown") or s.get("snippet") or "")
    s2 = dict(s)
    if text:
        ner = await _nlp("nlp.ner", text=text[:20000], task="gliner", labels=ops.topic_labels(t), threshold=0.4)
        ents = ops.ner_entities(ner)
        if ents:
            s2["entities"] = ents
            s2["entity_types"] = ops.entity_types(ner, ents)
            if config().get("geocode", True):
                places = []
                for pn in geo.places_in(s2["entity_types"]):
                    pl = await geo_lookup(pn)
                    if pl:
                        places.append({k: pl[k] for k in ("name", "lat", "lon", "country", "cc")} | {"as": pn})
                if places:
                    s2["places"] = places
        ev = []
        for q in (t.get("questions") or [])[:4]:
            a = ops.qa_answer(q, await _nlp("nlp.qa", question=q, context=text[:6000]))
            if a:
                ev.append(a)
        if ev:
            s2["evidence"] = ev
        sent = ops.stance(await _nlp("nlp.classify", text=(s.get("snippet") or text)[:500], task="sentiment3"))
        if sent:
            s2["stance"] = sent
    comps = ops.competitor_hits(f"{s.get('title', '')} {text[:4000]}", s2.get("entities") or [],
                                config().get("competitors") or [])
    if comps:
        s2["competitors"] = comps
    return s2, page


async def enrich_topic(t: Dict[str, Any], *, limit: int = 0, should_continue=None) -> Dict[str, Any]:
    """Every unread candidate gets the cheap NLP pass; only those scoring at the
    gate get a model to write their summary. Below the gate a source is still
    useful - it keeps its entities, answers and an extractive summary."""
    st = store()
    n = limit or core.DEPTH_BUDGET.get(t.get("depth") or "standard", core.DEPTH_BUDGET["standard"])["enrich"]
    gate = float(config().get("llm_gate", 5.0))
    todo = [s for s in st.signals(t["id"], since=_since(24 * 14), limit=200, enriched=False, order="score")
            if s.get("kind") not in ("dream", "research") and int(s.get("enrich_failed") or 0) < 2][: n * 3]
    llm_done = nlp_done = 0
    for s in todo:
        if await _stop_reason(should_continue):
            break
        s2, page = await _read_and_tag(t, s)
        low_genre = s2.get("genre") in ops.LOW_GENRES and not (s2.get("genre") == "marketing" and t.get("vendors"))
        to_model = not ((s2.get("score") or 0) < gate or llm_done >= n or low_genre)
        _decide("intel.enrich_worth", "model" if to_model else "nlp_only", state=ledger.signal_state(s2, t["id"]),
                ref_kind="signal", ref_id=s2["id"], decider="rule", mode="rule",
                detail={"score": s2.get("score"), "gate": gate, "budget_left": n - llm_done, "low_genre": low_genre})
        if not to_model:
            text = str(page.get("markdown") or s.get("snippet") or "")
            s2.update(summary=_extractive_summary(text) or s.get("snippet", ""), enriched=True, nlp_only=True,
                      enriched_at=core.now_iso())
            if page:
                s2["digest"] = core.reader_digest(page)
            st.put_signal(s2)
            nlp_done += 1
            continue
        triaged = bool(s2.get("entities")) or s2.get("relevance_nlp") is not None
        parsed = await _llm_json(core.enrich_prompt(t, s2, page.get("markdown") or "", triaged=triaged), role="enrich")
        if not parsed:
            st.put_signal(dict(s2, enrich_failed=int(s.get("enrich_failed") or 0) + 1))
            continue
        before = s2.get("score")
        s3 = core.apply_enrichment(s2, parsed, page=page or None)
        if before is not None and s3.get("score") is not None and s2.get("relevance_nlp") is not None:
            s3["score"] = round((float(before) + float(s3["score"])) / 2, 1)   # the model and the reranker agree or average
        st.put_signal(s3)
        llm_done += 1
        await _event("signal.enriched", topic=t["id"], id=s3["id"], title=s3["title"], score=s3.get("score"))
    if llm_done or nlp_done:
        await _to_fabric([st.get_signal(s["id"]) for s in todo])
    return {"topic": t["id"], "enriched": llm_done, "nlp_only": nlp_done, "candidates": len(todo)}


async def snapshot_topic(t: Dict[str, Any]) -> Dict[str, Any]:
    """Update the topic's assessment from the evidence since the last one (the MoE
    writes it), then decide whether the change is worth interrupting for."""
    st = store()
    prev = st.latest_snapshot(t["id"])
    since = (prev or {}).get("ts") or _since(24 * 7)
    sigs = [s for s in st.signals(t["id"], since=since, limit=60, order="score") if (s.get("score") or 0) >= 3]
    moves = _changes(t["id"], unassessed=True)
    if not sigs and not moves and prev:
        return {"topic": t["id"], "skipped": "no new evidence"}
    for s in sigs:                       # extractive answers ride along as evidence
        if s.get("evidence"):
            s["summary"] = (s.get("summary") or s.get("snippet") or "") + " | " + "; ".join(
                f"{e['question']} -> {e['answer']}" for e in s["evidence"][:3])
    levels = "\n".join(x for x in (chg.level_line(i) for i in _indicators(t["id"])) if x)
    parsed = await _llm_json(core.snapshot_prompt(t, prev, sigs, market=chg.prompt_lines(moves), levels=levels),
                             role="assess", timeout=1800.0)
    if not parsed:
        return {"topic": t["id"], "error": "no assessment"}
    snap = core.make_snapshot(t, parsed, sigs, first=not prev)
    fresh = st.get_topic(t["id"])
    if not fresh:                     # deleted while the model was thinking
        return {"topic": t["id"], "skipped": "topic deleted"}
    st.put_snapshot(snap)
    if moves:
        keys = {(c.get("indicator"), c.get("t")) for c in moves}
        st.set_kv("ind_changes", [dict(c, assessed=snap["id"]) if (c.get("indicator"), c.get("t")) in keys else c
                                  for c in st.get_kv("ind_changes", []) or []])
    qs = [q for q in fresh.get("questions") or [] if q not in {a["question"] for a in snap["answered"]}]
    qs += [q for q in snap["new_questions"] if q not in qs]
    st.patch_topic(t["id"], {"questions": qs[:12], "last_snapshot": snap["ts"]})
    await _event("snapshot.new", topic=t["id"], id=snap["id"], headline=snap["headline"],
                 changed=len(snap["changed"]))
    comps = sum(1 for s in sigs if s.get("competitors") and (s.get("score") or 0) >= 6)
    big = chg.material(moves)
    alert, why = ops.material_change(prev, snap, competitor_hits_n=comps, moves=[c.get("text") for c in big])
    if prev:
        _decide("intel.material_change", "alert" if alert else "no_alert",
                state={"topic": t["id"], "prev": prev.get("headline"), "now": snap.get("headline"),
                       "changed": snap.get("changed"), "comps": comps},
                ref_kind="snapshot", ref_id=snap["id"], decider="rule", mode="rule",
                detail={"changed": len(snap.get("changed") or []), "competitor_hits": comps, "moves": len(big)})
    if alert:
        await _alert(fresh, snap, why)
    if fresh.get("depth") == "deep" and config().get("auto_research") and snap["new_questions"]:
        pend = st.get_kv("research_wanted", []) or []
        for q in snap["new_questions"][:2]:
            if not any(p.get("query") == q for p in pend):
                pend.append({"topic_id": t["id"], "query": q, "added": core.now_iso()})
        st.set_kv("research_wanted", pend[-40:])
    return {"topic": t["id"], "snapshot": snap, "alert": why if alert else ""}


async def _alert(t: Dict[str, Any], snap: Dict[str, Any], why: str) -> None:
    """A material change goes out now (once per assessment), not in tomorrow's brief."""
    if not config().get("alerts", True):
        return
    st = store()
    sent = st.get_kv("alerts_sent", []) or []
    if snap["id"] in sent:
        return
    text = ops.alert_text(t["name"], snap, why, config().get("public_base", "").rstrip("/") + f"/intel/go/alert/{snap['id']}")
    for ch in config().get("alert_channels") or ["telegram"]:
        if ch == "telegram":
            await _call("tg.notify", text=text, idempotency_key=f"intel-alert-{snap['id']}")
        elif ch == "phone":
            await _call("ha.notify", target=config().get("phone_target"), title=f"Intel: {t['name']}"[:80],
                        message=text[:240])
    st.set_kv("alerts_sent", (sent + [snap["id"]])[-300:])
    st.set_kv("alerts", ((st.get_kv("alerts", []) or []) + [{"ts": core.now_iso(), "topic": t["id"], "why": why,
                                                            "headline": snap.get("headline")}])[-100:])
    await _event("alert", topic=t["id"], why=why, headline=snap.get("headline"))


@capability("intel.sweep", http_method="POST", http_path="/intel/sweep", http_tags=["intel"],
            description="Collect new signals now through discovery-first web search, scored by the NLP tier (no "
                        "model call). Input: topic_id (blank = every due topic), depth (override). "
                        "Output: {swept:[{topic,new,joined,queries}]}.")
async def cap_sweep(topic_id: str = "", depth: str = "", trace_id=None):
    st = store()
    topics = [st.get_topic(topic_id)] if topic_id else [t for t in st.topics("active") if core.is_due(t)]
    topics = [t for t in topics if t]
    if topic_id and not topics:
        return {"error": f"unknown topic: {topic_id}"}
    out = []
    for t in topics[: int(config().get("max_topics_per_night", 12)) if not topic_id else 1]:
        out.append(await sweep_topic(t, depth=depth))
    return {"swept": out, "message": f"{sum(x['new'] for x in out)} new signals"}


@capability("intel.refresh", http_method="POST", http_path="/intel/refresh", http_tags=["intel"],
            description="The full cycle for one topic: sweep, read and tag with the NLP tier, have a model summarise "
                        "the strongest, update the assessment. Input: topic_id (str!), queue (true = hand it to the "
                        "idle queue instead of running now). Output: {sweep, enrich, snapshot}.")
async def cap_refresh(topic_id: str = "", queue: bool = False, trace_id=None):
    t = store().get_topic(topic_id)
    if not t:
        return {"error": f"unknown topic: {topic_id}"}
    if queue:
        return await _submit_topic(t, reason="by hand", force=True)
    sw = await sweep_topic(t)
    t = store().get_topic(topic_id) or t
    en = await enrich_topic(t)
    sn = await snapshot_topic(t)
    return {"sweep": sw, "enrich": en, "snapshot": sn.get("snapshot") or sn,
            "message": f"{sw['new']} new, {en['enriched']} summarised, {en['nlp_only']} tagged"}


# ── indicators: the numbers a topic watches ──────────────────────────────────

def _epoch(ts: Any) -> Optional[float]:
    d = core.parse_ts(ts)
    return d.timestamp() if d else None


def _mkt_slug(symbol: str) -> str:
    """The markets subsystem's dataset slug (markets_data_capabilities._slug)."""
    return re.sub(r"[^a-z0-9]+", "_", (symbol or "").lower()).strip("_") or "asset"


def _indicators(topic_id: str = "") -> List[Dict[str, Any]]:
    allv = store().get_kv("indicators", {}) or {}
    if topic_id:
        return list(allv.get(topic_id) or [])
    return [i for v in allv.values() for i in v or []]


def _save_indicators(topic_id: str, inds: List[Dict[str, Any]]) -> None:
    st = store()
    allv = st.get_kv("indicators", {}) or {}
    if inds:
        allv[topic_id] = inds
    else:
        allv.pop(topic_id, None)
    st.set_kv("indicators", allv)


def _changes(topic_id: str = "", since: str = "", unassessed: bool = False) -> List[Dict[str, Any]]:
    """Indicator changes found so far (oldest first), optionally for one topic, only
    those whose bar falls at or after `since`, and with `unassessed` only those no
    assessment has taken in yet (however old the bar)."""
    out = store().get_kv("ind_changes", []) or []
    if topic_id:
        out = [c for c in out if c.get("topic_id") == topic_id]
    if since:
        lo = _epoch(since) or 0
        out = [c for c in out if float(c.get("t") or 0) >= lo]
    if unassessed:
        lo = time.time() - 30 * 86400
        out = [c for c in out if not c.get("assessed") and float(c.get("t") or 0) >= lo]
    return sorted(out, key=lambda c: float(c.get("t") or 0))


async def check_indicators(t: Dict[str, Any], *, lookback_days: int = 14) -> List[Dict[str, Any]]:
    """Read each indicator's stored bars, find the changes in the last
    `lookback_days`, and pin the topic's stories that landed around each one. No
    model call; a few reads and some arithmetic."""
    inds = _indicators(t["id"])
    if not inds:
        return []
    st = store()
    known = {(c.get("indicator"), c.get("t")) for c in st.get_kv("ind_changes", []) or []}
    stories = []
    for s in st.signals(t["id"], since=_since(24 * (lookback_days + 60)), limit=600):
        e = _epoch(s.get("published")) or _epoch(s.get("seen"))
        if e is not None:
            stories.append({"t": e, "id": s.get("id"), "title": s.get("title"), "url": s.get("url"),
                            "score": s.get("score")})
    new: List[Dict[str, Any]] = []
    for ind in inds:
        ts, vals, err = await _series(ind)
        if err or len(ts) < 25:
            ind["status"] = err or f"waiting for data ({len(ts)} bars)"
            continue
        now = time.time()
        stale_days = 60 if ind.get("kind") == "macro" else 10
        if ts[-1] < now - stale_days * 86400:
            ind.update(status="stale: newest bar " + time.strftime("%Y-%m-%d", time.gmtime(ts[-1])),
                       last=vals[-1], last_t=ts[-1], bars=len(ts), checked=core.now_iso())
            continue
        ind.update(status="ok", last=vals[-1], last_t=ts[-1], bars=len(ts), checked=core.now_iso(),
                   chg7=chg.change_over(ts, vals, 7), chg30=chg.change_over(ts, vals, 30))
        m = ind.get("market")
        if m and not ind.get("resolved"):
            ans = await _market_resolution(m)
            if ans:
                ind.update(resolved={"answer": ans, "at": core.now_iso()}, status=f"resolved {ans}")
                rc = dict(chg.resolution_change(ind, ans, now), indicator=ind["id"], topic_id=t["id"],
                          label=ind.get("label"), ind_kind=ind.get("kind"), stories=[], found=core.now_iso())
                new.append(rc)
                _record_resolution(t, ind, ans, chg.change_over(ts, vals, 7), vals[-1])
                continue
        if ind.get("resolved"):
            continue
        for c in chg.detect(ts, vals, kind=ind.get("kind", "price")):
            if float(c["t"]) < now - lookback_days * 86400 or (ind["id"], c["t"]) in known \
                    or (ind.get("market") and c["kind"] == "spike" and abs((c.get("detail") or {}).get("move") or 0) < 0.05):
                continue
            c.update(indicator=ind["id"], topic_id=t["id"], label=ind.get("label"), ind_kind=ind.get("kind"),
                     text=chg.describe(c, ind.get("label") or ind["dataset_id"], ind.get("kind", "price")),
                     stories=[{k: s.get(k) for k in ("id", "title", "url", "score", "lead_h")}
                              for s in chg.corroborate(float(c["t"]), stories)[:3]],
                     found=core.now_iso())
            new.append(c)
    fresh = {i["id"]: i for i in _indicators(t["id"])}          # an add/remove while we read wins
    _save_indicators(t["id"], [dict(fresh[i["id"]], **{k: i.get(k) for k in ("status", "last", "last_t", "bars", "checked",
                                                                              "chg7", "chg30", "resolved")
                                                        if k in i}) for i in inds if i["id"] in fresh])
    if new:
        st.set_kv("ind_changes", ((st.get_kv("ind_changes", []) or []) + new)[-400:])
        for c in new:
            await _event("indicator.change", topic=t["id"], indicator=c["indicator"], text=c["text"],
                         corroborated=len(c["stories"]))
    return new


@capability("intel.indicator.add", http_method="POST", http_path="/intel/indicator/add", http_tags=["intel"],
            description="Have a topic watch a number: a price (stock, crypto, FX, index - looked up and tracked "
                        "daily by the markets subsystem), a macro series, or any stored bar dataset. Its changes "
                        "(spikes, level shifts, regime changes) join the topic's timeline, its assessments and its "
                        "alerts, each with the stories that landed around it. Input: topic_id (str!), kind "
                        "(price|macro|probability|count), query (what to look up, or the macro series id), "
                        "dataset_id (an existing bar dataset instead), label; for a prediction market, kind "
                        "probability with market_source (polymarket|manifold) and market_id (from intel.pm.search "
                        "or intel.pm.suggest); for an exact listing, provider and symbol (from "
                        "intel.indicator.suggest). Output: {ok, indicator}.")
async def cap_indicator_add(topic_id: str = "", kind: str = "price", query: str = "", dataset_id: str = "",
                            label: str = "", market_source: str = "", market_id: str = "", provider: str = "",
                            symbol: str = "", trace_id=None):
    t = store().get_topic(topic_id)
    if not t:
        return {"error": f"unknown topic: {topic_id}"}
    kind = (kind or "price").lower()
    if kind not in chg.KINDS:
        return {"error": f"kind must be one of {', '.join(chg.KINDS)}"}
    ds, name, market = (dataset_id or "").strip(), "", None
    if kind == "probability" and not ds:
        if not (market_source and market_id):
            rows, _ = await pm_search(query, 5) if query else ([], [])
            return {"error": "a probability needs a market: pass market_source and market_id"
                             + (" - candidates below" if rows else ""), "candidates": rows}
        try:
            c = await _pm_market(market_source.strip().lower(), market_id.strip())
        except Exception as ex:  # noqa: BLE001
            return {"error": f"could not read that market: {type(ex).__name__}: {ex}"[:200]}
        if not c:
            return {"error": "that market is closed, resolved or not a yes/no market"}
        market = {k: c.get(k) for k in ("source", "id", "token", "question", "url", "end")}
        ds, name = pm.ref(c), pm.label(c)
    if not ds and kind == "price" and provider and symbol:
        add = await _call("markets.asset.add", provider=provider, symbol=symbol, timeframes=["1d"])
        if add.get("error"):
            return {"error": add["error"]}
        ds, name = f"mkt.{provider.lower()}.{_mkt_slug(symbol)}.1d", label or symbol
    if not ds:
        if not query:
            return {"error": "query or dataset_id required"}
        if kind == "macro":
            r = await _call("markets.macro.fetch", id=query, track=True)
            ds = ((r.get("datasets") or [""])[0]) if not r.get("error") else ""
            if not ds:
                return {"error": r.get("error") or f"no macro series for {query!r}"}
        elif kind == "price":
            lk = await _call("markets.lookup", query=query, limit=5)
            hits = [h for h in lk.get("results") or [] if h.get("provider") != "custom"]
            if not hits:
                return {"error": lk.get("error") or f"nothing found for {query!r}"}
            h = hits[0]
            add = await _call("markets.asset.add", provider=h["provider"], symbol=h["symbol"], timeframes=["1d"])
            if add.get("error"):
                return {"error": add["error"]}
            ds, name = f"mkt.{h['provider'].lower()}.{_mkt_slug(h['symbol'])}.1d", h.get("name") or h["symbol"]
        else:
            return {"error": f"a {kind} indicator needs a dataset_id"}
    inds = _indicators(topic_id)
    ind_id = hashlib.sha1(f"{topic_id}|{ds}".encode()).hexdigest()[:10]
    ind = {"id": ind_id, "topic_id": topic_id, "kind": kind, "dataset_id": ds, "query": query,
           "label": (label or name or query or ds)[:60], "added": core.now_iso(), "status": "new"}
    if market:
        ind["market"] = market
    inds = [i for i in inds if i["id"] != ind_id] + [ind]
    _save_indicators(topic_id, inds[:12])
    await _event("indicator.added", topic=topic_id, indicator=ind_id, label=ind["label"])
    return {"ok": True, "indicator": ind,
            "message": f"{t['name']} now watches {ind['label']} ({ds}); a new price fills in over the next minutes"}


# ── prediction markets: an open question with a price on it ──────────────────

async def _http_json(url: str, params: Optional[Dict[str, Any]] = None, timeout: float = 20.0,
                     headers: Optional[Dict[str, str]] = None) -> Any:
    """A public, keyless JSON read (prediction markets, the geocoder)."""
    import httpx
    async with httpx.AsyncClient(timeout=timeout, headers=headers or {"User-Agent": "vera-intel/1"},
                                 follow_redirects=True) as c:
        r = await c.get(url, params=params)
        r.raise_for_status()
        return r.json()


async def pm_search(query: str, limit: int = 8) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Open markets on Polymarket and Manifold for a question, best match first
    (the NLP tier's reranker over the market questions; by volume without it)."""
    cands: List[Dict[str, Any]] = []
    errors: List[str] = []
    try:
        cands += pm.polymarket_search(await _http_json(f"{pm.GAMMA}/public-search", {
            "q": query, "limit_per_type": 6, "events_status": "active"}), 20)
    except Exception as ex:  # noqa: BLE001
        errors.append(f"polymarket: {type(ex).__name__}: {ex}"[:200])
    try:
        cands += pm.manifold_search(await _http_json(f"{pm.MANIFOLD}/search-markets", {"term": query, "limit": 10}), 10)
    except Exception as ex:  # noqa: BLE001
        errors.append(f"manifold: {type(ex).__name__}: {ex}"[:200])
    if cands:
        rr = await _nlp("nlp.rerank", query=query, documents=[
            c["question"] + (f" ({c['event']})" if c.get("event") and c["event"] != c["question"] else "")
            for c in cands], top_k=len(cands))
        scores = {int(x.get("index", -1)): x.get("score") for x in rr.get("ranked") or []}
        for i, c in enumerate(cands):
            c["match"] = round(float(scores[i]), 2) if scores.get(i) is not None else None
        cands.sort(key=lambda c: (-(c["match"] if c.get("match") is not None else -99.0), -(c.get("volume") or 0)))
    return cands[:max(1, int(limit))], errors


async def _market_resolution(m: Dict[str, Any]) -> Optional[str]:
    """'yes'/'no' when a watched market has resolved (one small read per check)."""
    try:
        if m.get("source") == "polymarket":
            return pm.resolution("polymarket", await _http_json(f"{pm.GAMMA}/markets/{m.get('id')}"))
        if m.get("source") == "manifold":
            return pm.resolution("manifold", await _http_json(f"{pm.MANIFOLD}/market/{m.get('id')}"))
    except Exception as ex:  # noqa: BLE001
        log.debug("intel: market state unavailable: %s", ex)
    return None


def _record_resolution(t: Dict[str, Any], ind: Dict[str, Any], answer: str, chg7: Optional[float],
                       last: float) -> None:
    """A row for the calibration exhibit: how the market and intel's own assessment
    stood when the question was settled (not advice - a record of being right)."""
    st = store()
    snap = st.latest_snapshot(t["id"]) or {}
    week_before = None if chg7 is None else round(float(last) - float(chg7), 3)
    rows = st.get_kv("resolutions", []) or []
    rows.append({"topic_id": t["id"], "indicator": ind["id"], "question": (ind.get("market") or {}).get("question"),
                 "answer": answer, "at": core.now_iso(), "market_week_before": week_before,
                 "assessment": {"id": snap.get("id"), "confidence": snap.get("confidence"),
                                "headline": snap.get("headline")}})
    st.set_kv("resolutions", rows[-200:])


async def _pm_market(source: str, market_id: str) -> Optional[Dict[str, Any]]:
    if source == "polymarket":
        return pm.polymarket_market(await _http_json(f"{pm.GAMMA}/markets/{market_id}"))
    if source == "manifold":
        return pm.manifold_market(await _http_json(f"{pm.MANIFOLD}/market/{market_id}"))
    return None


async def _series(ind: Dict[str, Any]) -> Tuple[List[float], List[float], str]:
    """An indicator's (times, values) oldest first, or an error: a market's price
    history straight from its source (hourly), or stored bars from the markets
    subsystem (daily)."""
    m = ind.get("market")
    if m:
        try:
            if m.get("source") == "polymarket":
                pts = pm.polymarket_series(await _http_json(f"{pm.CLOB}/prices-history", {
                    "market": m.get("token"), "interval": "1m", "fidelity": 60}))
            else:
                pts = pm.manifold_series(await _http_json(f"{pm.MANIFOLD}/bets", {"contractId": m.get("id"), "limit": 1000}))
        except Exception as ex:  # noqa: BLE001
            return [], [], f"{type(ex).__name__}: {ex}"[:200]
        ts, vals = pm.resample(pts, 3600.0, until=time.time())
        return ts, vals, ""
    r = await _call("markets.bars", dataset_id=ind["dataset_id"], limit=50000)
    by_t: Dict[float, float] = {}
    for a, b in zip(r.get("t") or [], r.get("c") or []):   # markets.bars orders by insert time and leaves
        try:                                                # pre-2001 times in ms (RF-82): normalise, sort
            a, b = float(a), float(b)
        except (TypeError, ValueError):
            continue
        by_t[a / 1000.0 if a > 1e11 else a] = b
    pts = sorted(by_t.items())[-400:]
    return [p[0] for p in pts], [p[1] for p in pts], str(r.get("error") or "")


@capability("intel.pm.search", http_method="GET", http_path="/intel/pm/search", http_tags=["intel"],
            memory="off", silent=True,
            description="Find open prediction markets (Polymarket, Manifold) on a question - the price is the "
                        "crowd's probability. Input: query (str!), limit (default 8). Output: {rows:[{source, id, "
                        "question, event, prob, volume, end, url, match}], errors}.")
async def cap_pm_search(query: str = "", limit: int = 8, trace_id=None):
    if not (query or "").strip():
        return {"error": "query required"}
    rows, errors = await pm_search(query.strip(), limit)
    return {"rows": rows, "errors": errors}


@capability("intel.pm.suggest", http_method="GET", http_path="/intel/pm/suggest", http_tags=["intel"],
            memory="off", silent=True,
            description="Prediction markets that would measure a topic's open questions (no model call: search "
                        "plus the NLP reranker). Watch one with intel.indicator.add kind=probability. Input: "
                        "topic_id (str!), per_question (default 2). Output: {suggestions:[{question, markets}]}.")
async def cap_pm_suggest(topic_id: str = "", per_question: int = 2, trace_id=None):
    t = store().get_topic(topic_id)
    if not t:
        return {"error": f"unknown topic: {topic_id}"}
    watched = {i.get("dataset_id") for i in _indicators(topic_id)}
    out, errors = [], []
    for q in (t.get("questions") or [])[:6] or [t["name"]]:
        rows, errs = await pm_search(q, 6)
        errors += errs
        keep = [r for r in rows if pm.ref(r) not in watched and (r.get("match") is None or r["match"] > 0)]
        if keep:
            out.append({"question": q, "markets": keep[:max(1, int(per_question))]})
    return {"suggestions": out, "errors": sorted(set(errors))}


async def _lookup(name: str) -> List[Dict[str, Any]]:
    """markets.lookup, remembered for a week (names repeat night after night)."""
    st = store()
    cache = st.get_kv("lookup_cache", {}) or {}
    k = ops.org_key(name)
    hit = cache.get(k)
    if hit and (core.parse_ts(hit.get("at")) or datetime.min.replace(tzinfo=timezone.utc)) > \
            datetime.now(timezone.utc) - timedelta(days=7):
        return hit.get("results") or []
    r = await _call("markets.lookup", query=name, limit=5)
    res = [{k2: x.get(k2) for k2 in ("provider", "symbol", "name", "asset_class")} for x in r.get("results") or []][:5]
    if not r.get("error"):
        cache[k] = {"at": core.now_iso(), "results": res}
        if len(cache) > 500:
            cache = dict(sorted(cache.items(), key=lambda kv: kv[1].get("at") or "")[-400:])
        st.set_kv("lookup_cache", cache)
    return res


async def _refresh_suggestions(t: Dict[str, Any], *, every_days: int = 7) -> None:
    """Keep a topic's suggested numbers fresh (weekly) for the brief to offer."""
    st = store()
    have = st.get_kv(f"ind_suggest:{t['id']}", {}) or {}
    at = core.parse_ts(have.get("at"))
    if at and at > datetime.now(timezone.utc) - timedelta(days=every_days):
        return
    r = await cap_indicator_suggest(topic_id=t["id"])
    if not r.get("error"):
        st.set_kv(f"ind_suggest:{t['id']}", {"at": core.now_iso(), "prices": r.get("prices") or [],
                                             "markets": [{k: m.get(k) for k in ("source", "id", "question", "prob", "match")}
                                                         for m in r.get("markets") or []]})


def _watch_actions(t: Dict[str, Any], *, n: int = 2, mark: bool = True) -> List[Dict[str, Any]]:
    """Up to `n` suggested numbers as one-tap Watch buttons - each offered at most
    once a month, never one already watched."""
    st = store()
    sug = st.get_kv(f"ind_suggest:{t['id']}", {}) or {}
    watched = {i.get("dataset_id") for i in _indicators(t["id"])}
    watched_pm = {f"pm:{i['market'].get('source')}:{i['market'].get('id')}" for i in _indicators(t["id"]) if i.get("market")}
    offered = st.get_kv("watch_offered", {}) or {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    out: List[Dict[str, Any]] = []
    cands = [(f"price:{p['provider']}:{p['symbol']}", f"Watch {p.get('name') or p['symbol']}"[:70], p.get("name") or "",
              f"mkt.{p['provider'].lower()}.{_mkt_slug(p['symbol'])}.1d") for p in sug.get("prices") or []]
    cands += [(f"pm:{m['source']}:{m['id']}", f"Watch the odds: {m.get('question') or ''}"[:70], "",
               f"pm:{m['source']}:") for m in sug.get("markets") or [] if (m.get("match") or 0) > 1]
    for target, label, name, ds in cands:
        if len(out) >= n:
            break
        key = f"{t['id']}|{target}"
        last = core.parse_ts(offered.get(key))
        if (last and last > cutoff) or ds in watched or target in watched_pm:
            continue
        a = core.make_action("watch", label=label, topic_id=t["id"], target=target, query=name)
        if a:
            out.append(a)
            offered[key] = core.now_iso()
    if mark and out:
        st.set_kv("watch_offered", dict(sorted(offered.items(), key=lambda kv: kv[1])[-600:]))
    return out


@capability("intel.indicator.suggest", http_method="GET", http_path="/intel/indicator/suggest", http_tags=["intel"],
            memory="off", silent=True,
            description="Numbers a topic could watch, proposed from what its sources say (no model call): the "
                        "listed companies its sources name most (matched by name through markets.lookup) and open "
                        "prediction markets on the topic and its questions. Watch one with intel.indicator.add. "
                        "Input: topic_id (str!), days (signals window, default 60). Output: {prices:[{entity, "
                        "mentions, provider, symbol, name}], markets:[...], errors}.")
async def cap_indicator_suggest(topic_id: str = "", days: int = 60, trace_id=None):
    st = store()
    t = st.get_topic(topic_id)
    if not t:
        return {"error": f"unknown topic: {topic_id}"}
    watched = {i.get("dataset_id") for i in _indicators(topic_id)}
    sigs = st.signals(topic_id, since=_since(24 * max(1, int(days or 60))), limit=500)
    prices: List[Dict[str, Any]] = []
    coins = {e for sg in sigs for e, lab in (sg.get("entity_types") or {}).items() if lab == "cryptocurrency"}
    for ent, n in ops.org_mentions(sigs, follow=t.get("entities") or []):
        hit = ops.pick_listing(ent, await _lookup(ent), crypto=ent in coins)
        if not hit:
            continue
        ds = f"mkt.{hit['provider'].lower()}.{_mkt_slug(hit['symbol'])}.1d"
        if ds in watched or any(p["symbol"] == hit["symbol"] for p in prices):
            continue
        prices.append({"entity": ent, "mentions": None if n >= 999 else n, "followed": n >= 999,
                       "provider": hit["provider"], "symbol": hit["symbol"], "name": hit.get("name") or hit["symbol"]})
        if len(prices) >= 6:
            break
    markets: List[Dict[str, Any]] = []
    errors: List[str] = []
    for q in [t["name"]] + list(t.get("questions") or [])[:3]:
        rows, errs = await pm_search(q, 4)
        errors += errs
        for r in rows:
            if (r.get("match") is None or r["match"] > 0) and pm.ref(r) not in watched \
                    and all(m["id"] != r["id"] for m in markets):
                markets.append(dict(r, for_question=q))
    markets.sort(key=lambda m: -(m.get("match") if m.get("match") is not None else -99.0))
    return {"prices": prices, "markets": markets[:5], "errors": sorted(set(errors))}


_GEO_LOCK = asyncio.Lock()
_GEO_LAST = [0.0]


async def geo_lookup(name: str, cc: str = "") -> Optional[Dict[str, Any]]:
    """A place's coordinates: cached forever (a miss for 30 days), otherwise one
    Nominatim request, never more than one a second across the process."""
    key = geo.geo_key(name, cc)
    if not key:
        return None
    st = store()
    hit = st.get_kv(f"geo:{key}")
    if hit is not None:
        if hit.get("miss"):
            at = core.parse_ts(hit.get("at"))
            if at and at > datetime.now(timezone.utc) - timedelta(days=geo.MISS_TTL_DAYS):
                return None
        else:
            return hit
    async with _GEO_LOCK:
        wait = geo.MIN_INTERVAL_S - (time.time() - _GEO_LAST[0])
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            params = {"q": name, "format": "jsonv2", "limit": 1, "addressdetails": 1, "accept-language": "en"}
            if cc:
                params["countrycodes"] = cc
            res = geo.parse(await _http_json(geo.NOMINATIM, params, headers={"User-Agent": geo.USER_AGENT}), name)
        except Exception as ex:  # noqa: BLE001
            log.debug("intel: geocode failed for %s: %s", name, ex)
            return None
        finally:
            _GEO_LAST[0] = time.time()
    st.set_kv(f"geo:{key}", res or {"miss": True, "at": core.now_iso()})
    return res


@capability("geo.lookup", http_method="GET", http_path="/geo/lookup", http_tags=["intel", "geo"],
            memory="off", silent=True,
            description="A place name to coordinates (OpenStreetMap Nominatim, cached; at most one request a "
                        "second). Input: name (str!), cc (ISO country code to restrict to, optional). Output: "
                        "{ok, place:{name, lat, lon, bbox:[south,north,west,east], country, cc, admin, type}} or "
                        "{ok:false, error}.")
async def cap_geo_lookup(name: str = "", cc: str = "", trace_id=None):
    if not (name or "").strip():
        return {"error": "name required"}
    p = await geo_lookup(name.strip(), (cc or "").strip()[:2])
    return {"ok": True, "place": p} if p else {"ok": False, "error": f"no place found for {name!r}"}


@capability("intel.places.get", http_method="GET", http_path="/intel/places/get", http_tags=["intel", "geo"],
            memory="off", silent=True,
            description="Where a topic's stories happen: the places its signals name, as globe pins (one per place, "
                        "sized by how many signals name it, the strongest story as detail, lit when named in the "
                        "last 48 h). Input: topic_id (blank = every topic), days (default 30). Output: {points:[{name, "
                        "lat, lon, size, meta, detail, url, lit}]}.")
async def cap_places_get(topic_id: str = "", days: int = 30, trace_id=None):
    sigs = store().signals(topic_id, since=_since(24 * max(1, min(365, int(days or 30)))), limit=3000)
    pts = geo.pins([s for s in sigs if s.get("topic_id") != ops.NOTES_TOPIC], recent_after=time.time() - 48 * 3600)
    return {"points": pts, "title": "Where it is happening"}


@capability("intel.corpus.export", http_method="GET", http_path="/intel/corpus/export", http_tags=["intel"],
            memory="off", silent=True,
            description="intel's signals as a curated corpus for the worldview (four-minds FM-8.3/8.4): "
                        "timestamped, entity-tagged, scored records about the outside world (never the owner's "
                        "notes or files), no site above max_share of the records, plus per-topic story and "
                        "assessment sequences in observed order. Input: since (ISO, default 90 days), topics "
                        "(list; blank = all), max_share (0.15), min_score (0), limit (5000), vectors (false - "
                        "include the MiniLM-384 embedding). Output: {schema, records, sequences, stats}.")
async def cap_corpus_export(since: str = "", topics: Any = None, max_share: float = 0.15, min_score: float = 0.0,
                            limit: int = 5000, vectors: bool = False, trace_id=None):
    st = store()
    since = since or _since(24 * 90)
    tids = set(_list_arg(topics))
    sigs = [s for s in st.signals("", since=since, limit=max(1, min(20000, int(limit or 5000))))
            if not tids or s.get("topic_id") in tids]
    rows, stats = ops.corpus_rows(sigs, max_share=float(max_share), min_score=float(min_score or 0),
                                  private_topics=[ops.NOTES_TOPIC])
    if vectors:
        vec = {s["id"]: s.get("vec") for s in sigs if s.get("vec")}
        for r in rows:
            if vec.get(r["id"]):
                r["vec"] = vec[r["id"]]
    snaps = [sn for sn in st.snapshots("", since=since, limit=5000)
             if (not tids or sn.get("topic_id") in tids) and sn.get("topic_id") != ops.NOTES_TOPIC]
    return {"schema": ops.CORPUS_SCHEMA, "since": since, "records": rows,
            "sequences": ops.sequences(rows, snaps), "stats": stats}


@capability("intel.indicator.list", http_method="GET", http_path="/intel/indicator/list", http_tags=["intel"],
            memory="off", silent=True,
            description="The numbers a topic watches (blank topic = all) with their latest value, 7/30-day change "
                        "and recent changes, plus resolved prediction markets with how the market and intel's "
                        "assessment stood. Input: topic_id, days (changes window, default 30). Output: {indicators, "
                        "changes, resolutions}.")
async def cap_indicator_list(topic_id: str = "", days: int = 30, trace_id=None):
    res = [r for r in store().get_kv("resolutions", []) or [] if not topic_id or r.get("topic_id") == topic_id]
    return {"indicators": _indicators(topic_id),
            "changes": list(reversed(_changes(topic_id, since=_since(24 * max(1, int(days or 30))))))[:100],
            "resolutions": list(reversed(res))[:50]}


@capability("intel.indicator.remove", http_method="POST", http_path="/intel/indicator/remove", http_tags=["intel"],
            description="Stop a topic watching a number. Input: topic_id (str!), indicator_id (str!). Output: {ok}.")
async def cap_indicator_remove(topic_id: str = "", indicator_id: str = "", trace_id=None):
    inds = _indicators(topic_id)
    keep = [i for i in inds if i["id"] != indicator_id]
    if len(keep) == len(inds):
        return {"error": f"no indicator {indicator_id} on {topic_id}"}
    _save_indicators(topic_id, keep)
    st = store()
    st.set_kv("ind_changes", [c for c in st.get_kv("ind_changes", []) or [] if c.get("indicator") != indicator_id])
    return {"ok": True}


@capability("intel.indicator.check", http_method="POST", http_path="/intel/indicator/check", http_tags=["intel"],
            description="Look for changes in a topic's numbers now (no model call; the nightly unit does this "
                        "too). Input: topic_id (blank = every topic with indicators). Output: {changes}.")
async def cap_indicator_check(topic_id: str = "", trace_id=None):
    st = store()
    tids = [topic_id] if topic_id else sorted({i["topic_id"] for i in _indicators()})
    out: List[Dict[str, Any]] = []
    for tid in tids:
        t = st.get_topic(tid)
        if t:
            out += await check_indicators(t)
    return {"changes": out, "message": f"{len(out)} new change(s)"}


# ── reports ──────────────────────────────────────────────────────────────────

async def build_report(topic_ids: List[str], *, period_hours: int = 24, title: str = "",
                       only_new: bool = True, synthesize: bool = False, note: str = "") -> Dict[str, Any]:
    """Build (and store) a report from what is gathered. `synthesize` lets the MoE
    write the cross-topic top line and follow-ups - only ever from background work
    (the nightly batch), never at delivery time."""
    st = store()
    topics = [st.get_topic(i) for i in topic_ids] if topic_ids else st.topics("active")
    topics = [t for t in topics if t]
    since = _since(period_hours)
    told = set(st.get_kv("reported_snapshots", []) or []) if only_new else set()
    per: Dict[str, Dict[str, Any]] = {}
    competitors: List[Dict[str, Any]] = []
    for t in topics:
        sigs = st.signals(t["id"], since=since, limit=80, order="score", unreported=only_new)
        for s in sigs:
            for firm in s.get("competitors") or []:
                if (s.get("score") or 0) >= 4:
                    competitors.append({"firm": firm, "title": s.get("title"), "url": s.get("url"),
                                        "topic": t["name"], "score": s.get("score")})
        sigs = [s for s in sigs if (s.get("score") or 0) >= float(t.get("min_score", 5))]
        snap = st.latest_snapshot(t["id"])
        if snap and core.parse_ts(snap.get("ts")) and core.parse_ts(snap["ts"]) < core.parse_ts(since):
            snap = None                       # an old assessment is not news
        if snap and snap.get("id") in told:
            snap = None                       # nor is one already sent
        moves = _changes(t["id"], since=since)
        if not sigs and not snap and not moves:
            continue
        acts = core.actions_for(t, sigs, snap) + _watch_actions(t, mark=only_new)
        per[t["id"]] = {"signals": sigs, "snapshot": snap, "actions": acts, "moves": moves}
    topics = [t for t in topics if t["id"] in per]
    notes = list(st.get_kv("notes_report", []) or []) if (only_new and not topic_ids) else []
    if config().get("illustrate") and topics:
        for t in topics[:2]:
            imgs = await _call("media.illustrate", subject=t["name"], count=1)
            for im in (imgs.get("images") or ([imgs] if imgs.get("url") else []))[:1]:
                if im.get("url"):
                    per[t["id"]].setdefault("images", []).append({"url": im["url"], "caption": im.get("caption") or t["name"]})
    doc = core.build_report(topics, per, title=title, period_hours=period_hours)
    if note:
        doc["note"] = note
    seen_c = set()
    doc["competitors"] = [c for c in sorted(competitors, key=lambda c: -(c.get("score") or 0))
                          if (c["firm"], c["url"]) not in seen_c and not seen_c.add((c["firm"], c["url"]))][:12]
    if notes:
        doc["notes"] = notes[:20]
    if synthesize and topics:
        sections = [{"topic": t["name"], "headline": (per[t["id"]].get("snapshot") or {}).get("headline", ""),
                     "changed": (per[t["id"]].get("snapshot") or {}).get("changed", []),
                     "signals": per[t["id"]]["signals"][:6]} for t in topics]
        parsed = await _llm_json(ops.brief_prompt(core.now_iso()[:10], sections, doc["competitors"]),
                                 role="brief", timeout=1800.0)
        doc = ops.apply_brief(doc, parsed)
        fu = []
        for q in doc.get("follow_ups") or []:
            a = core.make_action("research", label=f"Research: {q[:70]}", query=q)
            if a:
                fu.append(a)
        if fu:
            doc["follow_up_actions"] = [{k: a.get(k) for k in ("token", "kind", "label", "glyph", "query")} for a in fu]
            for a in fu:
                st.put_action(dict(a, report_id=doc["id"]), report_id=doc["id"])
    if not _has_content(doc):
        doc["empty"] = True
        return doc                    # nothing to say: never stored, so the report list holds only real briefs
    for tid, d in per.items():
        for a in d["actions"]:
            st.put_action(dict(a, report_id=doc["id"]), report_id=doc["id"])
    html = core.render_html(doc, base_url="")
    st.put_report(doc, html)
    if only_new:                      # a look-back report must not eat the next brief
        st.mark_reported(s["id"] for d in per.values() for s in d["signals"])
    if only_new and (doc["sections"] or notes):
        sent = [d["snapshot"]["id"] for d in per.values() if d.get("snapshot")]
        st.set_kv("reported_snapshots", (list(told) + sent)[-500:])
        if notes:
            st.set_kv("notes_report", [])
    await _event("report.new", id=doc["id"], title=doc["title"], signals=doc["counts"]["signals"])
    if config().get("speak", True):
        await _submit("speak", f"intel: read aloud - {doc['title'][:50]}", key=f"speak:{doc['id']}",
                      payload={"report_id": doc["id"]}, priority=60)
    return doc


def _has_content(doc: Dict[str, Any]) -> bool:
    return bool(doc.get("sections") or doc.get("notes") or doc.get("competitors"))


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
           "empty": not _has_content(doc)}
    chans = _list_arg(deliver)
    if chans and _has_content(doc):
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


def _audio_dir() -> Path:
    from Vera.vera.state_paths import state_dir
    return state_dir("intel", "audio")


async def _tts(text: str, voice: str = "", speed: float = 1.0) -> bytes:
    """One piece of narration from the media node's speech server -> WAV bytes."""
    import base64
    import httpx
    base = _orch.media_base("tts")
    hdr = _orch.vera_origin_header(job_type="tts", cap="intel.speak") if hasattr(_orch, "vera_origin_header") else {}
    body: Dict[str, Any] = {"text": text, "speed": speed}
    if voice:
        body["voice"] = voice
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post(f"{base}/tts", headers=hdr, json=body)
        r.raise_for_status()
        b64 = (r.json() or {}).get("audio_b64") or ""
    if not b64:
        raise RuntimeError("the speech server returned no audio")
    return base64.b64decode(b64)


async def speak_report(rid: str, *, should_continue=None) -> Dict[str, Any]:
    """Read a report aloud: each chapter of its spoken script through the speech
    server, stitched into one file with chapter start times, attached to the report
    (the page then shows a player). Background work: the idle queue runs it."""
    import io
    import wave
    st = store()
    doc = st.get_report(re.sub(r"[^a-f0-9]", "", rid or ""))
    if not doc:
        return {"ok": False, "error": f"unknown report: {rid}"}
    script = core.spoken_script(doc)
    if not script:
        return {"ok": True, "skipped": "nothing to read"}
    c = config()
    params, frames, chapters, pos = None, [], [], 0
    for ch in script:
        why = await _stop_reason(should_continue)
        if why:
            return {"yielded": "pre-empted: " + why}
        chapters.append({"id": ch["id"], "title": ch["title"], "start": round(pos / params[2], 1) if params else 0.0})
        for chunk in ch["chunks"]:
            try:
                wav = await _tts(chunk, c.get("speak_voice", ""), float(c.get("speak_speed", 1.0) or 1.0))
                with wave.open(io.BytesIO(wav), "rb") as w:
                    p = (w.getnchannels(), w.getsampwidth(), w.getframerate())
                    data = w.readframes(w.getnframes())
            except Exception as ex:  # noqa: BLE001
                return {"ok": False, "error": f"speech failed: {type(ex).__name__}: {ex}"[:200]}
            if params is None:
                params = p
            if p != params:                     # one voice, one format; a stray piece is skipped, not mangled
                continue
            frames.append(data)
            pos += len(data) // (p[0] * p[1])
            frames.append(b"\x00" * (int(p[2] * 0.35) * p[0] * p[1]))     # a breath between pieces
            pos += int(p[2] * 0.35)
    if not params or not frames:
        return {"ok": False, "error": "no audio"}
    d = _audio_dir()
    wav_path = d / f"{doc['id']}.wav"
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(params[0]); w.setsampwidth(params[1]); w.setframerate(params[2])
        w.writeframes(b"".join(frames))
    path = wav_path
    if shutil.which("ffmpeg"):
        mp3 = d / f"{doc['id']}.mp3"
        proc = await asyncio.create_subprocess_exec("ffmpeg", "-y", "-loglevel", "error", "-i", str(wav_path),
                                                    "-codec:a", "libmp3lame", "-qscale:a", "5", str(mp3),
                                                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        if await proc.wait() == 0 and mp3.exists():
            wav_path.unlink(missing_ok=True)
            path = mp3
    doc = st.get_report(doc["id"]) or doc               # the record may have moved on while we spoke
    doc["audio"] = {"url": f"/intel/report/audio/{doc['id']}", "seconds": round(pos / params[2], 1),
                    "chapters": chapters, "format": path.suffix.lstrip("."), "made": core.now_iso()}
    st.put_report(doc, core.render_html(doc))
    await _event("report.audio", id=doc["id"], seconds=doc["audio"]["seconds"])
    return {"ok": True, "seconds": doc["audio"]["seconds"], "chapters": len(chapters)}


@APP.get("/intel/report/audio/{rid}", include_in_schema=False)
async def _serve_audio(rid: str):
    from fastapi.responses import FileResponse
    rid = re.sub(r"[^a-f0-9]", "", rid)
    for ext, mt in (("mp3", "audio/mpeg"), ("wav", "audio/wav")):
        p = _audio_dir() / f"{rid}.{ext}"
        if rid and p.exists():
            return FileResponse(str(p), media_type=mt, filename=f"intel-{rid}.{ext}")
    return Response(status_code=404)


@capability("intel.report.speak", http_method="POST", http_path="/intel/report/speak", http_tags=["intel"],
            description="Read a report aloud: queue (default) or make now the narration of its updates - top line, "
                        "each topic's headline, what changed and what moved - with chapter marks; the report page "
                        "then has play/pause, seek and speed, and a Listen button per topic. Input: id (str!), now "
                        "(bool, default false - true speaks immediately). Output: {ok, queued|seconds}.")
async def cap_report_speak(id: str = "", now: bool = False, trace_id=None):
    if not store().get_report(re.sub(r"[^a-f0-9]", "", id or "")):
        return {"error": f"unknown report: {id}"}
    if now:
        return await speak_report(id)
    return await _submit("speak", "intel: read aloud", key=f"speak:{id}", payload={"report_id": id}, priority=60)


@APP.get("/intel/report/view/{rid}", include_in_schema=False)
async def _serve_report(rid: str):
    d = store().get_report(re.sub(r"[^a-f0-9]", "", rid), with_html=True)
    if not d:
        return HTMLResponse("<h1>No such report</h1>", status_code=404)
    d.pop("_html", None)
    return HTMLResponse(core.render_html(d), headers={"Cache-Control": "no-cache"})


# ── delivery ─────────────────────────────────────────────────────────────────

async def deliver_report(rid: str, channels: List[str], *, target: str = "") -> Dict[str, Any]:
    st = store()
    doc = st.get_report(rid, with_html=True)
    if not doc:
        return {"error": f"unknown report: {rid}", "results": []}
    doc.pop("_html", None)
    html_doc = core.render_html(doc)              # current rendering, not the one stored at build time
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
        tgt = target
        if ch == "phone" and not tgt:
            tgt = str(config().get("phone_target") or "")
        if ch == "print" and not tgt:
            tgt = str(config().get("printer_id") or "")
        args = core.channel_args(ch, doc, report_url=public, action_url=action_url,
                                 html_doc=html_doc if ch == "email" else "", target=tgt)
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
    try:
        await _event("action.started", kind=a["kind"], label=a["label"], topic=a.get("topic_id"))
        r = await _call(a["cap"], **args)
    except Exception as ex:  # noqa: BLE001 - never leave a claimed action 'running'
        r = {"error": f"{type(ex).__name__}: {ex}"}
    good = not r.get("error")
    st.finish_action(token, "done" if good else "failed", {k: r.get(k) for k in list(r)[:12]})
    if good and a.get("target") and a.get("topic_id"):
        tgt = core.canonical_url(a["target"])
        for sig in st.signals(a["topic_id"], limit=500):
            if sig.get("url") == tgt:
                _outcome("signal", sig["id"], "report.button")
                break
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
    fresh = not (dest / ".git").exists()
    if fresh:
        cmds = [["git", "clone", "--depth", "1", "--no-recurse-submodules", "--single-branch",
                 "-c", "core.hooksPath=/dev/null", r["clone"], str(dest)]]
    else:                             # a shallow clone cannot fast-forward: fetch the tip and move to it
        cmds = [["git", "-C", str(dest), "fetch", "--depth", "1", "origin", "HEAD"],
                ["git", "-C", str(dest), "reset", "--hard", "FETCH_HEAD"]]
    for cmd in cmds:
        try:
            p = await asyncio.create_subprocess_exec(*cmd, env=env, stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError:
            return {"error": "git is not installed here"}
        try:
            _out, err = await asyncio.wait_for(p.communicate(), timeout=300)
        except asyncio.TimeoutError:
            try:
                p.kill()
            except ProcessLookupError:
                pass
            if fresh:                 # never leave a half clone that a later call mistakes for one
                shutil.rmtree(dest, ignore_errors=True)
            return {"error": "git timed out after 300s"}
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


# ── background work: one idle-queue job per unit, any time the box is quiet ──
#
# Units: a topic refresh, the drop folders, the automation advisor, a research
# question. The nightly batch is a set of topic units plus a brief built when the
# last one finishes; daytime and session work submit the same units. The idle
# queue starts a unit only after real quiet, and every unit checks between steps.

def _svc():
    try:
        from Vera.vera import idle_queue_service as s
        return s
    except Exception:  # noqa: BLE001
        return None


async def _submit(mode: str, title: str, *, key: str, payload: Optional[Dict[str, Any]] = None,
                  priority: Optional[int] = None) -> Dict[str, Any]:
    svc = _svc()
    body = dict(payload or {}, mode=mode)
    if svc is None:
        asyncio.create_task(_intel_job({"payload": body}, None))
        return {"ok": True, "queued": False, "message": "idle queue unavailable - running now"}
    r = await svc.submit(KIND_INTEL, title, dedupe_key=f"intel:{key}", payload=body, total=1, priority=priority)
    if not r.get("queued"):
        return {"ok": True, "queued": False, "job": r, "message": f"not queued: {r.get('reason') or 'refused'}"}
    return {"ok": True, "queued": True, "job": r, "message": "queued for the next quiet window"}


async def _submit_topic(t: Dict[str, Any], *, reason: str, force: bool = False, until: str = "",
                        batch: str = "", priority: Optional[int] = None) -> Dict[str, Any]:
    return await _submit("topic", f"intel: {t['name'][:50]} ({reason})",
                         key=f"topic:{t['id']}" + (f":{batch}" if batch else ""),
                         payload={"topic_id": t["id"], "force": force, "until": until, "batch": batch},
                         priority=priority)


async def _progress(job: Dict[str, Any], done: int, total: int) -> None:
    svc = _svc()
    if svc is not None and job.get("id"):
        try:
            await svc.report_progress(job["id"], done=done, total=total)
        except Exception:  # noqa: BLE001
            pass


def _past(until: str) -> bool:
    u = core.parse_ts(until)
    return bool(u and datetime.now(timezone.utc) >= u)


async def _intel_job(job: Dict[str, Any], should_continue) -> Dict[str, Any]:
    p = job.get("payload") or {}
    mode = p.get("mode") or ("topic" if p.get("topic_id") else "night")
    if p.get("until") and _past(p["until"]):
        return {"ok": True, "skipped": "its session ended"}       # dropped, not re-queued
    if mode == "topic":
        return await _job_topic(job, p, should_continue)
    if mode == "files":
        return await run_drop_folders(should_continue)
    if mode == "advise":
        return await run_advisor()
    if mode == "research":
        return await _job_research(p)
    if mode == "sweeps":
        return await _job_sweeps(job, p, should_continue)
    if mode == "speak":
        return await speak_report(p.get("report_id", ""), should_continue=should_continue)
    if mode == "brief":
        await _finish_night(p.get("night", ""))
        return {"ok": True}
    if mode == "night":                # an old-style job left in the queue: start a batch instead
        await _start_night(_night_of(_local_now(), int(config().get("sweep_hour", 23))))
        return {"ok": True, "converted": "night batch"}
    return {"ok": False, "error": f"unknown intel job mode {mode}"}


async def _job_topic(job: Dict[str, Any], p: Dict[str, Any], should_continue) -> Dict[str, Any]:
    """One topic unit. Whatever happens to it - done, failed, gone - the night
    batch hears it is finished, so a batch can never wait on a unit forever; only
    a yield (which the queue runs again) leaves it pending."""
    res: Dict[str, Any] = {}
    try:
        res = await _job_topic_inner(job, p, should_continue)
        return res
    except Exception as ex:  # noqa: BLE001
        log.warning("intel: topic unit failed: %s", ex)
        res = {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
        return res
    finally:
        if not str(res.get("yielded") or ""):
            await _batch_done(p.get("batch") or (store().get_kv("night", {}) or {}).get("id"),
                              p.get("topic_id", ""), res.get("_stats") or {})
            res.pop("_stats", None)


async def _job_topic_inner(job: Dict[str, Any], p: Dict[str, Any], should_continue) -> Dict[str, Any]:
    st = store()
    t = st.get_topic(p.get("topic_id", ""))
    if not t or (t.get("status") != "active" and not p.get("force")):
        return {"ok": True, "skipped": "topic gone or paused"}
    why = await _stop_reason(should_continue)
    if why:
        return {"yielded": "pre-empted: " + why}
    out: Dict[str, Any] = {"topic": t["id"]}
    night = st.get_kv("night", {}) or {}
    if p.get("batch") and night.get("id") == p["batch"] and t["id"] in (night.get("swept") or []):
        sw = {"new": 0, "queries": [], "skipped": "swept in phase 1"}      # counted by the sweeps job
    else:
        sw = await _sweep_and_check(t)
    out["new"] = sw["new"]
    await _progress(job, 1, 3)
    if await _stop_reason(should_continue) or (p.get("until") and _past(p["until"])):
        return {"yielded": "pre-empted after sweep", **out}
    en = await enrich_topic(st.get_topic(t["id"]) or t, should_continue=should_continue)
    out.update(assessed=en["enriched"], tagged=en["nlp_only"])
    await _progress(job, 2, 3)
    if await _stop_reason(should_continue) or (p.get("until") and _past(p["until"])):
        return {"yielded": "pre-empted after reading", **out}
    sn = await snapshot_topic(st.get_topic(t["id"]) or t)
    out["alert"] = sn.get("alert", "")
    await _progress(job, 3, 3)
    return {"ok": True, **out, "_stats": {"searched": len(sw.get("queries") or []), "new": sw["new"],
                                           "assessed": en["enriched"]}}


async def _sweep_and_check(t: Dict[str, Any]) -> Dict[str, Any]:
    """The model-free half of a topic's night: sweep and triage, read its numbers,
    refresh its suggestions."""
    sw = await sweep_topic(t)
    try:
        await check_indicators(store().get_topic(t["id"]) or t)
    except Exception as ex:  # noqa: BLE001
        log.warning("intel: indicator check failed for %s: %s", t["id"], ex)
    try:
        await _refresh_suggestions(t)
    except Exception as ex:  # noqa: BLE001
        log.warning("intel: suggestions failed for %s: %s", t["id"], ex)
    return sw


async def _job_sweeps(job: Dict[str, Any], p: Dict[str, Any], should_continue) -> Dict[str, Any]:
    """Phase 1 of a night: sweep EVERY topic (search, NLP triage, numbers - no
    model) before any topic gets model time, so a night cut short by interactive
    use still briefs fresh signals for all of them. Progress lives on the night
    record, so a yield resumes where it stopped."""
    st = store()
    ids = list(p.get("topic_ids") or [])
    done = 0
    for tid in ids:
        n = st.get_kv("night", {}) or {}
        if n.get("id") != p.get("night"):
            return {"ok": True, "skipped": "a newer night started"}
        if tid in (n.get("swept") or []):
            done += 1
            continue
        why = await _stop_reason(should_continue)
        if why:
            return {"yielded": "pre-empted: " + why, "swept": done}
        t = st.get_topic(tid)
        if t and t.get("status") == "active":
            try:
                sw = await _sweep_and_check(t)
            except Exception as ex:  # noqa: BLE001 - one bad topic must not stop the phase
                log.warning("intel: phase-1 sweep failed for %s: %s", tid, ex)
                sw = {"new": 0, "queries": []}
            n = st.get_kv("night", {}) or {}
            if n.get("id") == p.get("night"):
                agg = n.get("stats") or {}
                agg["searched"] = agg.get("searched", 0) + len(sw.get("queries") or [])
                agg["new"] = agg.get("new", 0) + int(sw.get("new") or 0)
                n["stats"], n["swept"] = agg, (n.get("swept") or []) + [tid]
                st.set_kv("night", n)
        done += 1
        await _progress(job, done, len(ids))
    return {"ok": True, "swept": done}


async def _job_research(p: Dict[str, Any]) -> Dict[str, Any]:
    """Start a research run for a question (it runs as its own job); its result
    comes back as a signal at the topic's next sweep."""
    q, tid = p.get("query", ""), p.get("topic_id", "")
    r = await _call("research.run", query=q, mode="single", output_mode="report")
    if r.get("error") or not r.get("job_id"):
        return {"ok": False, "error": r.get("error") or "research did not start"}
    st = store()
    jobs = st.get_kv("research_jobs", {}) or {}
    jobs[str(r["job_id"])] = {"topic_id": tid, "query": q, "started": core.now_iso()}
    st.set_kv("research_jobs", jobs)
    return {"ok": True, "job_id": r["job_id"]}


# the nightly batch: its topics, and the brief when the last of them is done

async def _start_night(night: str) -> Dict[str, Any]:
    st = store()
    c = config()
    due = [t for t in st.topics("active") if core.is_due(t)]
    due.sort(key=lambda t: (t.get("depth") != "deep", t.get("last_sweep") or ""))
    due = due[: int(c.get("max_topics_per_night", 12))]
    st.set_kv("night", {"id": night, "pending": [t["id"] for t in due], "stats": {"topics": len(due)},
                        "started": core.now_iso(), "brief": "", "swept": []})
    if due:                      # phase 1: every topic's sweep (no model) before any topic's model work
        await _submit("sweeps", f"intel: sweep {len(due)} topics (no model)", key=f"sweeps:{night}",
                      payload={"night": night, "topic_ids": [t["id"] for t in due]}, priority=25)
    if c.get("drop_folders"):
        await _submit("files", "intel: drop folders", key=f"files:{night}")
    for t in due:
        r = await _submit_topic(t, reason="night", batch=night)
        if not r.get("queued", True):
            await _batch_done(night, t["id"], {})           # refused (already queued): never wait on it
    n = st.get_kv("night", {}) or {}
    if n.get("id") == night and not n.get("pending") and not n.get("brief"):
        await _submit("brief", "intel: write the brief", key=f"brief:{night}", payload={"night": night}, priority=20)
    return {"topics": len(due)}


async def _batch_done(batch: Optional[str], topic_id: str, stats: Dict[str, Any]) -> None:
    if not batch:
        return
    st = store()
    n = st.get_kv("night", {}) or {}
    if n.get("id") != batch or topic_id not in (n.get("pending") or []):
        return
    n["pending"] = [x for x in n.get("pending") or [] if x != topic_id]
    agg = n.get("stats") or {}
    for k, v in (stats or {}).items():
        agg[k] = agg.get(k, 0) + (v or 0)
    n["stats"] = agg
    st.set_kv("night", n)
    if not n["pending"]:
        await _submit("brief", "intel: write the brief", key=f"brief:{batch}", payload={"night": batch}, priority=20)


async def _finish_night(night: str) -> None:
    """The batch is in: the MoE writes the brief's top line now (still night,
    still background); the morning tick only delivers it."""
    st = store()
    n = st.get_kv("night", {}) or {}
    if n.get("brief"):
        return
    doc = await build_report([], period_hours=int(config().get("report_period_hours", 24)), synthesize=True)
    n["brief"] = doc["id"] if _has_content(doc) else "-"
    st.set_kv("night", n)
    if _has_content(doc):
        if st.get_kv("brief_day") == _local_now().date().isoformat():
            # the morning already went out: this one is late - one quiet message, not a second fan-out
            await deliver_report(doc["id"], ["html", "telegram"])
        else:
            st.set_kv("pending_brief", {"id": doc["id"], "ts": doc["ts"]})
    st.set_kv("last_nightly", {"ts": core.now_iso(), "topics": (n.get("stats") or {}).get("topics", 0),
                               "stats": n.get("stats") or {}})
    if config().get("night_summary"):
        await _call("tg.notify", text=ops.night_summary(n.get("stats") or {}),
                    idempotency_key=f"intel-night-{night}")


try:
    _svc_reg = _svc()
    if _svc_reg is not None:
        _svc_reg.register_handler(KIND_INTEL, _intel_job)
except Exception as _ex:  # noqa: BLE001
    log.info("intel: idle queue handler not registered: %s", _ex)


def _local_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(config().get("timezone") or "Europe/London"))
    except Exception:  # noqa: BLE001
        return datetime.now()


def _in_window(hour: int, start: int, end: int) -> bool:
    """start <= hour < end on a clock that wraps at midnight (23 -> 7 works)."""
    if start == end:
        return False
    return start <= hour < end if start < end else (hour >= start or hour < end)


def _night_of(now: datetime, start: int) -> str:
    """The night a time belongs to, named by the date it began on, so a window
    that crosses midnight is still one night."""
    d = now.date() if now.hour >= start else (now - timedelta(days=1)).date()
    return d.isoformat()


def _budget_left(key: str, limit: int) -> bool:
    b = store().get_kv("budget", {}) or {}
    return b.get("day") != _local_now().date().isoformat() or int(b.get(key, 0)) < limit


def _bump_daily(key: str, limit: int) -> bool:
    """Count one unit of today's budget; False when it is spent."""
    st = store()
    today = _local_now().date().isoformat()
    b = st.get_kv("budget", {}) or {}
    if b.get("day") != today:
        b = {"day": today}
    if int(b.get(key, 0)) >= limit:
        return False
    b[key] = int(b.get(key, 0)) + 1
    st.set_kv("budget", b)
    return True


async def _plan_session(s: Dict[str, Any], now: datetime) -> int:
    """Fill an open session with work, up to its budget: stale assessments first,
    then research on deep topics' open questions, then anything due."""
    st = store()
    key = f"{s['name']}@{s['ends'].isoformat()[:16]}"     # one occurrence of the window
    runs = st.get_kv("session_runs", {}) or {}
    used = int(runs.get(key, 0))
    budget = int(s.get("budget", 6))
    if used >= budget:
        return 0
    until = s["ends"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    planned = 0
    topics = st.topics("active")
    stale = sorted(topics, key=lambda t: t.get("last_snapshot") or "")
    if s.get("mode") in ("deepen", "refresh"):
        for t in stale:
            if used + planned >= budget:
                break
            if s.get("mode") == "refresh" and not core.is_due(t):
                continue
            r = await _submit_topic(t, reason=f"session {s['name']}", force=True, until=until, priority=40)
            if r.get("queued", True):
                planned += 1
    if s.get("mode") in ("deepen", "research") and config().get("auto_research"):
        wanted = st.get_kv("research_wanted", []) or []
        left = []
        for w in wanted:
            if used + planned >= budget:
                left.append(w)
                continue
            r = await _submit("research", f"intel research: {w['query'][:50]}", key=f"research:{core.slug(w['query'])}",
                              payload={"topic_id": w["topic_id"], "query": w["query"], "until": until}, priority=45)
            if r.get("queued", True):
                planned += 1
            else:
                left.append(w)                 # refused: keep it for later
        st.set_kv("research_wanted", left)
    runs[key] = used + planned
    st.set_kv("session_runs", dict(list(runs.items())[-60:]))
    return planned


async def _tick() -> None:
    """Every few minutes. Night: queue the batch once. Morning: deliver the
    brief (never generate it). Monday: the week in review. Sunday: the advisor.
    Any time: due topics as idle-queue units within the daily budget, and an open
    session's planned work. Nothing here runs a model - the queue does, when quiet."""
    c = config()
    if not c.get("enabled"):
        return
    st = store()
    now = _local_now()
    today = now.date().isoformat()
    sh, bh = int(c.get("sweep_hour", 23)), int(c.get("brief_hour", 7))
    night = _night_of(now, sh)
    in_night = _in_window(now.hour, sh, bh)
    if in_night and st.get_kv("nightly_day") != night and st.topics("active"):
        st.set_kv("nightly_day", night)
        await _start_night(night)
    if bh <= now.hour < bh + 4 and st.get_kv("brief_day") != today:
        st.set_kv("brief_day", today)
        pend = st.get_kv("pending_brief") or {}
        rid = pend.get("id")
        if not rid:              # the night did not finish: brief what is in, and say so
            doc = await build_report([], period_hours=int(c.get("report_period_hours", 24)),
                                     note=ops.night_note(st.get_kv("night", {}) or {}))
            rid = doc["id"] if _has_content(doc) else ""
        if rid:
            await deliver_report(rid, list(c.get("brief_channels") or ["html"]))
        st.set_kv("pending_brief", {})
    if now.weekday() == int(c.get("weekly_day", 0)) and now.hour >= int(c.get("weekly_hour", 8)) \
            and st.get_kv("weekly_day_done") != today:
        st.set_kv("weekly_day_done", today)
        doc = await build_report([], period_hours=168, title=f"Week in review - {today}", only_new=False)
        if _has_content(doc):
            await deliver_report(doc["id"], [ch for ch in (c.get("brief_channels") or ["html"])
                                               if ch not in ("podcast", "print")])
    week = f"{now.isocalendar()[0]}-{now.isocalendar()[1]}"
    if now.weekday() == int(c.get("advise_day", 6)) and now.hour >= int(c.get("advise_hour", 3)) \
            and st.get_kv("advise_week") != week:
        st.set_kv("advise_week", week)
        await _submit("advise", "intel: automation proposals", key=f"advise:{week}")
    if c.get("opportunistic") and not in_night:
        for t in st.topics("active"):
            if t.get("cadence") in ("twice-daily",) and core.is_due(t) \
                    and _budget_left("opportunistic", int(c.get("daily_job_budget", 24))):
                r = await _submit_topic(t, reason="due")
                if r.get("queued", True):
                    _bump_daily("opportunistic", int(c.get("daily_job_budget", 24)))
    s = ops.active_session(c.get("sessions") or [], now)
    if s:
        await _plan_session(s, now)
    if st.get_kv("decisions_pruned") != today:
        st.set_kv("decisions_pruned", today)
        st.prune_decisions(_since(24 * 90))


schedule(_tick, 300, name="intel.tick", skip_in_sandbox=True, singleton=True)


# ── drop folders (notes / inbox / print / podcast) ──────────────────────────

def _notes_topic() -> Dict[str, Any]:
    st = store()
    t = st.get_topic(ops.NOTES_TOPIC)
    if not t:
        t = core.normalise_topic({"id": ops.NOTES_TOPIC, "name": ops.N8N_GROUP_NAMES["notes"],
                                  "brief": "Searches drawn from your notes and inbox", "origin": "notes"})
        st.put_topic(t)
    return t


async def run_drop_folders(should_continue=None) -> Dict[str, Any]:
    """What files-night did: read the drop folders; notes are diffed (only real
    edits reach a model) and their search terms feed the notes topic; inbox
    documents are summarised into the fabric; print and podcast folders are
    printed / narrated once per content."""
    st = store()
    c = config()
    host = c.get("files_host") or ""
    work: List[Dict[str, Any]] = []
    for f in c.get("drop_folders") or []:
        kind, path = f.get("kind"), str(f.get("path") or "").rstrip("/")
        if kind not in ops.FOLDER_KINDS or not path:
            continue
        ls = await _call("fs.list", ssh_host_id=host, path=path)
        for e in ls.get("entries") or []:
            if e.get("type") != "file" or not ops.wanted_file(kind, str(e.get("name") or ""), e.get("size")):
                continue
            fp = f"{path}/{e['name']}"
            rd = await _call("fs.read", ssh_host_id=host, path=fp, max_bytes=ops.MAX_FILE_BYTES)
            if not (rd.get("ok") or rd.get("text")):
                continue
            h = ops.content_hash(rd.get("text") or "")
            if kind != "notes" and st.get_kv(f"seen-n8n:{fp}#{ops.cyrb53(ops.norm_text(rd.get('text') or ''))}"):
                if not st.get_kv(f"seen:{fp}#{h}"):
                    st.set_kv(f"seen:{fp}#{h}", core.now_iso())   # files-night handled exactly this content
                continue
            item = ops.plan_file(kind, fp, rd.get("text") or "", st.get_kv(f"note:{fp}") if kind == "notes" else None,
                                 bool(st.get_kv(f"seen:{fp}#{h}")))
            if item:
                work.append(item)
    budget = int(c.get("files_model_budget", 8))
    stats = {"notes": 0, "inbox": 0, "printed": 0, "podcasts": 0, "deferred": 0}
    lines: List[str] = []
    for item in work:
        if await _stop_reason(should_continue):
            return {"yielded": "pre-empted: drop folders", **stats}
        k, now = item["kind"], core.now_iso()
        if k.startswith("note_"):
            parsed = None
            if item.get("llm"):
                if budget <= 0:
                    stats["deferred"] += 1
                    continue
                budget -= 1
                parsed = await _llm_json(ops.note_prompt(item), role="notes")
            rec, terms, line = ops.apply_note(item, parsed, now)
            st.set_kv(f"note:{item['path']}", rec)
            if terms:
                nt = _notes_topic()
                st.patch_topic(nt["id"], {"queries": list(dict.fromkeys((nt.get("queries") or []) + terms))[-40:]})
            if line:
                stats["notes"] += 1
                lines.append(line)
        elif k == "inbox":
            if budget <= 0:
                stats["deferred"] += 1
                continue
            budget -= 1
            p = await _llm_json(ops.note_prompt(item), role="notes") or {}
            if not p.get("summary"):
                tries = int(st.get_kv(f"tries:{item['path']}#{item['hash']}") or 0) + 1
                st.set_kv(f"tries:{item['path']}#{item['hash']}", tries)
                if tries < 3:
                    continue
            fab = sys.modules.get("data_fabric")
            if fab and getattr(fab, "ingest_dataset", None):
                try:
                    await fab.ingest_dataset(dataset_id="files.inbox", data=[{
                        "path": item["path"], "name": item["name"], "hash": item["hash"], "text": item["norm"][:60000],
                        "title": str(p.get("title") or item["name"]), "summary": str(p.get("summary") or ""),
                        "doc_type": str(p.get("doc_type") or ""), "entities": p.get("entities") or [],
                        "action_items": p.get("action_items") or [], "keep": bool(p.get("keep")), "at": now}],
                        source="intel", tags=["inbox"], source_id="intel", defer_embedding=True)
                except Exception as ex:  # noqa: BLE001
                    log.info("intel: inbox to fabric failed: %s", ex)
            terms = [q for q in (p.get("search_terms") or []) if isinstance(q, str) and len(q) > 3]
            if terms:
                nt = _notes_topic()
                st.patch_topic(nt["id"], {"queries": list(dict.fromkeys((nt.get("queries") or []) + terms))[-40:]})
            st.set_kv(f"seen:{item['path']}#{item['hash']}", now)
            stats["inbox"] += 1
            lines.append(f"inbox: {p.get('title') or item['name']} - {str(p.get('summary') or '(no summary)')[:160]}")
        elif k == "print":
            r = await _call("print.text", title=item["name"][:24], text=item["norm"][:3000],
                            printer_id=c.get("printer_id") or "", cut=True)
            if not r.get("error"):
                st.set_kv(f"seen:{item['path']}#{item['hash']}", now)
                stats["printed"] += 1
                lines.append(f"printed {item['name']}")
        elif k == "podcast":
            if stats["podcasts"] >= 1:
                stats["deferred"] += 1
                continue
            r = await _call("background.cap.enqueue", name="podcast.generate", title=f"podcast: {item['name']}",
                            submitted_by="intel", id=f"intel-podcast-{item['hash']}", timeout_s=3600,
                            arguments={"topic": re.sub(r"\.[^.]+$", "", item["name"]),
                                       "sources": [{"type": "text", "text": item["norm"][:20000]}], "wait": True})
            if not r.get("error"):
                st.set_kv(f"seen:{item['path']}#{item['hash']}", now)
                stats["podcasts"] += 1
                lines.append(f"podcast queued: {item['name']}")
    if lines:
        st.set_kv("notes_report", ((st.get_kv("notes_report", []) or []) + lines)[-40:])
    n = st.get_kv("night", {}) or {}
    if n:
        n.setdefault("stats", {})["notes"] = n.get("stats", {}).get("notes", 0) + stats["notes"]
        st.set_kv("night", n)
    return {"ok": True, **stats, "files": len(work)}


@capability("intel.files.run", http_method="POST", http_path="/intel/files/run", http_tags=["intel"],
            description="Read the drop folders now (notes are diffed, inbox summarised, print/podcast folders acted on). "
                        "Input: queue (default true - hand it to the idle queue). Output: {notes, inbox, printed, podcasts}.")
async def cap_files_run(queue: bool = True, trace_id=None):
    if queue:
        return await _submit("files", "intel: drop folders (by hand)", key=f"files:{core.now_iso()[:13]}")
    return await run_drop_folders(None)


@capability("intel.notes.list", http_method="GET", http_path="/intel/notes/list", http_tags=["intel"],
            memory="off", silent=True,
            description="Notes the drop folders hold, with what intelligence made of them (summary, action items, "
                        "questions, search terms). Output: {rows}.")
async def cap_notes_list(trace_id=None):
    import sqlite3
    rows = []
    try:
        with sqlite3.connect(store().path) as c:
            for k, v in c.execute("SELECT k, v FROM kv WHERE k LIKE 'note:%'").fetchall():
                r = json.loads(v)
                r.pop("norm", None)
                r["meta"] = f"{len(r.get('action_items') or [])} actions · {str(r.get('updated', ''))[:10]}"
                r["name"] = r.get("title") or r.get("name")
                rows.append(r)
    except Exception as ex:  # noqa: BLE001
        return {"error": str(ex), "rows": []}
    rows.sort(key=lambda r: str(r.get("updated") or ""), reverse=True)
    return {"rows": rows, "count": len(rows)}


# ── the automation advisor ───────────────────────────────────────────────────

async def run_advisor() -> Dict[str, Any]:
    """What intel-automations did: inventory what runs, read the week's tooling
    intelligence, ask the MoE for 3-5 proposals, compile each into an INACTIVE
    n8n draft, and publish the list."""
    st = store()
    wf = (await _call("n8n.workflow.list", limit=250)).get("workflows") or []
    svc = [str(x.get("label") or "") for x in (await _call("integration.list")).get("integrations") or []]
    svc = list(dict.fromkeys(s for s in svc if s and not re.search(r"vera-dev-|sbxw-|-worktree-", s)))
    intel = [s for s in st.signals("", since=_since(24 * 7), limit=200, order="score") if (s.get("score") or 0) >= 6]
    prev = [p.get("title", "") for p in (st.get_kv("proposals", []) or [])]
    parsed = await _llm_json(ops.advisor_prompt(wf, svc, intel, prev), role="advise", timeout=1800.0)
    sugg = [x for x in ((parsed or {}).get("suggestions") or []) if isinstance(x, dict)][:5]
    if not sugg:
        return {"ok": False, "error": "no proposals from the model"}
    vera_url = config().get("public_base", "").rstrip("/") + "/mcp/call"
    made = []
    today = core.now_iso()[:10]
    for sg in sugg:
        draft = ops.compile_draft(sg, vera_url, today)
        r = await _call("n8n.workflow.create", activate=False, **draft)
        made.append({"date": today, "title": str(sg.get("title") or ""), "category": str(sg.get("category") or "new"),
                     "rationale": str(sg.get("rationale") or ""), "risk": str(sg.get("risk") or ""),
                     "effort": str(sg.get("effort") or ""), "trigger": str(sg.get("trigger") or ""),
                     "steps": [{"name": s.get("name"), "cap": s.get("cap"), "note": s.get("note")}
                               for s in sg.get("steps") or [] if isinstance(s, dict)],
                     "draft_id": r.get("id") or "", "error": r.get("error") or ""})
    st.set_kv("proposals", ((st.get_kv("proposals", []) or []) + made)[-60:])
    md = "\n".join([f"# Automation proposals {today}", "",
                    f"{len(made)} proposals, each compiled into an INACTIVE n8n draft (manual trigger)."] + [
        "\n".join([f"## {m['title']}  `{m['category']}`", "", m["rationale"], "",
                   f"- Trigger: {m['trigger']} | risk: {m['risk']} | effort: {m['effort']}",
                   f"- Draft: {m['draft_id'] or 'FAILED ' + m['error']}", "",
                   "| # | Step | Capability | Does |", "|---|---|---|---|"] +
                  [f"| {i + 1} | {s.get('name')} | `{s.get('cap')}` | {str(s.get('note') or '').replace('|', '/')} |"
                   for i, s in enumerate(m["steps"])]) for m in made])
    page = await _call("report.html", content=md, title=f"Automation proposals {today}", mode="pandoc")
    url = page.get("url") or ""
    if url and not url.startswith("http"):
        url = config().get("public_base", "").rstrip("/") + url
    await _call("tg.notify", text="\n".join([f"Automation proposals ({len(made)}), drafted inactive in n8n:", ""] +
                                            [f"- {m['title']} [{m['category']}, {m['risk']} risk]" for m in made] +
                                            ["", url or "(page failed)"]), idempotency_key=f"intel-advise-{today}")
    return {"ok": True, "proposals": len(made), "url": url}


@capability("intel.advise", http_method="POST", http_path="/intel/advise", http_tags=["intel"],
            description="Automation proposals from the week's intelligence: 3-5 ideas, each compiled into an INACTIVE "
                        "n8n draft for review. Input: queue (default true). Output: {proposals, url}.")
async def cap_advise(queue: bool = True, trace_id=None):
    if queue:
        return await _submit("advise", "intel: automation proposals (by hand)", key=f"advise:{core.now_iso()[:13]}")
    return await run_advisor()


@capability("intel.proposals.list", http_method="GET", http_path="/intel/proposals/list", http_tags=["intel"],
            memory="off", silent=True, description="Automation proposals made so far. Output: {rows}.")
async def cap_proposals_list(trace_id=None):
    rows = list(reversed(store().get_kv("proposals", []) or []))
    for r in rows:
        r["name"] = r.get("title")
        r["meta"] = f"{r.get('category')} · {r.get('date')}"
    return {"rows": rows, "count": len(rows)}


# ── save, command, sessions ──────────────────────────────────────────────────

@capability("intel.save", http_method="POST", http_path="/intel/save", http_tags=["intel"],
            description="Save a page to read later: read in reader mode, tagged by the NLP tier, kept as a signal on a "
                        "topic (default the Saved topic). Input: url (str!), topic_id, note. Output: {ok, signal}.")
async def cap_save(url: str = "", topic_id: str = "", note: str = "", trace_id=None):
    if not re.match(r"(?i)^https?://", url or ""):
        return {"error": "an http(s) url is required"}
    st = store()
    tid = topic_id or ops.SAVED_TOPIC
    t = st.get_topic(tid)
    if not t:
        if topic_id:
            return {"error": f"unknown topic: {topic_id}"}
        t = core.normalise_topic({"id": ops.SAVED_TOPIC, "name": "Saved", "cadence": "manual",
                                  "brief": "Pages saved to read later", "min_score": 0})
        st.put_topic(t)
    s = core.normalise_signal(t["id"], {"url": url, "title": url, "snippet": note}, source="saved")
    if not s:
        return {"error": "unusable url"}
    s2, page = await _read_and_tag(t, s)
    if page.get("title"):
        s2["title"] = str(page["title"])[:300]
    text = str(page.get("markdown") or "")
    s2.update(score=7.0, enriched=True, nlp_only=True, summary=_extractive_summary(text) or note,
              digest=core.reader_digest(page) if page else {}, enriched_at=core.now_iso())
    st.put_signal(s2)
    await _to_fabric([s2])
    return {"ok": True, "signal": {k: s2.get(k) for k in ("id", "title", "url", "summary", "entities")}}


@capability("intel.command", http_method="POST", http_path="/intel/command", http_tags=["intel"],
            description="Drive intelligence with one line of text (Telegram, phone shortcut): " + ops.HELP +
                        ". Input: text (str!). Output: {ok, text}.")
async def cap_command(text: str = "", trace_id=None):
    st = store()
    cmd, args = ops.parse_command(text)
    if cmd == "list":
        rows = st.topics("active")
        return {"ok": True, "text": "\n".join(f"{t['id']}: {t['name']} ({t['depth']}, {t['cadence']})" for t in rows)
                or "no topics"}
    if cmd == "status":
        s = await cap_summary()
        n = st.get_kv("last_nightly", {}) or {}
        return {"ok": True, "text": f"{s['topics']} topics, {s['signals_24h']} new today, {s['reports_7d']} reports this "
                                    f"week, {s['actions_open']} actions open; last night: {n.get('ts', 'never')}"}
    if cmd in ("add", "track"):
        name, _, q = args.partition("::")
        if not name.strip():
            return {"ok": False, "text": "usage: add <topic> [:: <search>]"}
        r = await cap_topic_save(name=name.strip(), queries=[q.strip()] if q.strip() else None, origin="command")
        return {"ok": not r.get("error"), "text": r.get("error") or f"tracking {r['topic']['name']} ({r['topic']['id']})"}
    if cmd == "remove":
        t = st.get_topic(args.strip())
        if not t:
            return {"ok": False, "text": f"unknown topic: {args}"}
        st.patch_topic(t["id"], {"status": "paused"})
        return {"ok": True, "text": f"paused {t['name']} (its history is kept)"}
    if cmd == "sweep":
        if args.strip():
            t = st.get_topic(args.strip())
            if not t:
                return {"ok": False, "text": f"unknown topic: {args}"}
            r = await _submit_topic(t, reason="command", force=True)
            return {"ok": True, "text": f"{t['name']}: {r['message']}"}
        n = 0
        for t in st.topics("active"):
            if core.is_due(t):
                await _submit_topic(t, reason="command")
                n += 1
        return {"ok": True, "text": f"{n} due topics queued"}
    if cmd == "night":
        cur = st.get_kv("night", {}) or {}
        if cur.get("pending") or (cur.get("id") and not cur.get("brief")):
            return {"ok": False, "text": f"a batch is still running ({len(cur.get('pending') or [])} topics left)"}
        r = await _start_night(f"cmd-{core.now_iso()[:16]}")
        return {"ok": True, "text": f"night batch queued: {r['topics']} topics"}
    if cmd == "files":
        r = await cap_files_run(queue=True)
        return {"ok": True, "text": r.get("message", "queued")}
    if cmd == "brief":
        doc = await build_report([], period_hours=int(config().get("report_period_hours", 24)))
        if not _has_content(doc):
            return {"ok": True, "text": "nothing new since the last brief"}
        await deliver_report(doc["id"], ["telegram"])
        return {"ok": True, "text": f"brief sent: {_report_url(doc['id'], public=True)}"}
    if cmd == "advise":
        r = await cap_advise(queue=True)
        return {"ok": True, "text": r.get("message", "queued")}
    if cmd == "save":
        parts = args.split()
        if not parts:
            return {"ok": False, "text": "usage: save <url> [topic id]"}
        r = await cap_save(url=parts[0], topic_id=parts[1] if len(parts) > 1 else "")
        return {"ok": not r.get("error"), "text": r.get("error") or f"saved: {r['signal']['title']}"}
    return {"ok": True, "text": ops.HELP}


@capability("intel.session.status", http_method="GET", http_path="/intel/session/status", http_tags=["intel"],
            memory="off", silent=True,
            description="Intelligence sessions: the windows configured, whether one is open now and when it ends, what "
                        "has been planned into it, and research waiting for one. Output: {sessions, active, runs, wanted}.")
async def cap_session_status(trace_id=None):
    st = store()
    now = _local_now()
    s = ops.active_session(config().get("sessions") or [], now)
    return {"sessions": [x for x in (ops.normalise_session(r) for r in config().get("sessions") or []) if x],
            "active": dict(s, ends=s["ends"].isoformat()) if s else None,
            "runs": st.get_kv("session_runs", {}) or {}, "wanted": st.get_kv("research_wanted", []) or [],
            "budget": st.get_kv("budget", {}) or {}, "alerts": (st.get_kv("alerts", []) or [])[-10:],
            "spend_today": st.get_kv("spend:" + datetime.now(timezone.utc).strftime("%Y-%m-%d"), {}) or {},
            "night": {k: v for k, v in (st.get_kv("night", {}) or {}).items() if k != "pending"}}


@capability("intel.config.get", http_method="GET", http_path="/intel/config", http_tags=["intel"],
            memory="off", silent=True,
            description="Intelligence settings: schedule, delivery, persona, competitors, drop folders, sessions, NLP "
                        "and model gates. Output: {config, schedule, profile}.")
async def cap_config_get(trace_id=None):
    st = store()
    return {"config": config(), "profile": "intel" if _PROFILE["declared"] else "",
            "schedule": {"nightly_day": st.get_kv("nightly_day"), "brief_day": st.get_kv("brief_day"),
                         "pending_brief": st.get_kv("pending_brief"), "last_nightly": st.get_kv("last_nightly"),
                         "weekly_day_done": st.get_kv("weekly_day_done"), "advise_week": st.get_kv("advise_week")}}


def _clean_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in (raw or {}).items():
        if k not in _CONFIG_KEYS or v is None:
            continue
        d = DEFAULT_CONFIG[k]
        if k in ("brief_channels", "alert_channels"):
            v = [c for c in _list_arg(v) if c in core.CHANNELS]
        elif k == "competitors":
            v = _list_arg(v)[:60]
        elif k == "sessions":
            v = [x for x in (ops.normalise_session(s) for s in (v if isinstance(v, list) else [])
                             if isinstance(s, dict)) if x][:12]
        elif k == "drop_folders":
            v = [{"kind": f.get("kind"), "path": str(f.get("path") or "")} for f in (v if isinstance(v, list) else [])
                 if isinstance(f, dict) and f.get("kind") in ops.FOLDER_KINDS and f.get("path")][:12]
        elif isinstance(d, bool):
            v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
        elif isinstance(d, int):
            v = int(v)
        elif isinstance(d, float):
            v = float(v)
        else:
            v = str(v)
        if k in ("sweep_hour", "brief_hour", "weekly_hour", "advise_hour"):
            v = max(0, min(23, v))
        if k in ("weekly_day", "advise_day"):
            v = max(0, min(6, v))
        out[k] = v
    return out


@capability("intel.config.set", http_method="POST", http_path="/intel/config", http_tags=["intel"],
            description="Change intelligence settings. Input: settings (object of any keys intel.config.get shows - "
                        "e.g. {\"sessions\": [{\"name\": \"Saturday research\", \"days\": [5], \"start\": \"09:00\", "
                        "\"end\": \"13:00\", \"budget\": 8, \"mode\": \"deepen\"}], \"competitors\": [\"Savills\"]}), or the "
                        "common ones directly: enabled, sweep_hour, brief_hour, brief_channels, public_base, illustrate. "
                        "Output: {ok, config}.")
async def cap_config_set(settings: Any = None, enabled: bool = None, timezone: str = None, sweep_hour: int = None,
                         brief_hour: int = None, max_topics_per_night: int = None, report_period_hours: int = None,
                         brief_channels: Any = None, public_base: str = None, illustrate: bool = None, trace_id=None):
    if isinstance(settings, str) and settings.strip():
        try:
            settings = json.loads(settings)
        except ValueError:
            return {"error": "settings must be a JSON object"}
    raw = dict(settings or {}) if isinstance(settings, dict) else {}
    for k, v in (("enabled", enabled), ("timezone", timezone), ("sweep_hour", sweep_hour), ("brief_hour", brief_hour),
                 ("max_topics_per_night", max_topics_per_night), ("report_period_hours", report_period_hours),
                 ("brief_channels", brief_channels), ("public_base", public_base), ("illustrate", illustrate)):
        if v is not None:
            raw[k] = v
    try:
        clean = _clean_config(raw)
    except (TypeError, ValueError) as e:
        return {"error": f"bad setting: {e}"}
    cur = store().get_kv("config", {}) or {}
    cur.update(clean)
    store().set_kv("config", cur)
    return {"ok": True, "config": config(), "changed": sorted(clean)}


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
            description="Bring the n8n intelligence state into Vera: topics from vera.interests, vera.monitors (as deep "
                        "topics) and intel.sources (grouped and named as the n8n brief named them); assessed items from "
                        "intel.enrichments; the notes files-night already read (so they are not read again); the "
                        "competitors its enrichment named. Idempotent, never overwrites local edits. Input: dry_run "
                        "(default true). Output: {topics, signals, notes, competitors, dry_run}.")
async def cap_import_n8n(dry_run: bool = True, trace_id=None):
    st = store()
    plans: Dict[str, Dict[str, Any]] = {}
    for it in _latest_by(await _browse_all("vera.interests"), "topic", "created").values():
        if it.get("active") is False or not it.get("topic"):
            continue
        plans[core.slug(it["topic"])] = {"name": it["topic"], "brief": it.get("note", ""), "queries": [],
                                         "origin": "n8n:vera.interests"}
    for m in _latest_by(await _browse_all("vera.monitors"), "topic", "created").values():
        if m.get("active") is False or not m.get("topic"):
            continue
        p = plans.setdefault(core.slug(m["topic"]), {"name": m["topic"], "brief": "", "queries": [],
                                                     "origin": "n8n:vera.monitors"})
        p["depth"] = "deep"
    groups: Dict[str, List[str]] = {}
    for src in _latest_by(await _browse_all("intel.sources"), "id", "added").values():
        if not src.get("query") or src.get("enabled") is False or src.get("enabled") == "false":
            continue
        groups.setdefault(str(src.get("topic") or "general"), []).append(src["query"])
    for grp, qs in groups.items():
        name = ops.N8N_GROUP_NAMES.get(grp, grp.replace("_", " ").title())
        tid = ops.NOTES_TOPIC if grp == "notes" else core.slug(name)
        p = plans.setdefault(tid, {"name": name, "brief": "", "queries": [], "origin": f"n8n:intel.sources:{grp}"})
        p["queries"] = list(dict.fromkeys(p["queries"] + qs))[:40]
        p["n8n_group"] = grp
    sigs, firms = [], []
    for en in _latest_by(await _browse_all("intel.enrichments"), "url", "enriched_at").values():
        comp = str(en.get("competitor") or "").strip()
        if comp and comp.lower() not in ("none", "n/a") and comp not in firms:
            firms.append(comp)
        grp = str(en.get("topic") or "")
        tid = next((k for k, p in plans.items() if p.get("n8n_group") == grp), None)
        if not tid or not en.get("enriched"):
            continue
        s = core.normalise_signal(tid, {"url": en.get("url"), "title": en.get("title"),
                                        "snippet": en.get("summary"), "seen": en.get("enriched_at")}, source="n8n")
        if not s:
            continue
        s.update(summary=en.get("summary", ""), so_what=en.get("so_what", ""), tags=en.get("tags") or [],
                 entities=en.get("entities") or [], enriched=True, enriched_at=en.get("enriched_at"),
                 score=float(en["relevance"]) if isinstance(en.get("relevance"), (int, float)) else None,
                 competitors=[comp] if comp and comp.lower() not in ("none", "n/a") else [], reported=True)
        sigs.append(s)
    notes = list(_latest_by(await _browse_all("intel.notes"), "path", "updated").values())
    seen = [r for r in await _browse_all("files.seen") if r.get("path") and r.get("hash")]
    if not dry_run:
        for tid, p in plans.items():
            existing = st.get_topic(tid)
            raw = {k: v for k, v in p.items() if v not in ("", None, []) and k != "n8n_group"}
            if existing:              # a re-import adds searches; it never overwrites what was edited here
                raw.pop("brief", None)
                raw.pop("origin", None)
                raw.pop("depth", None)
                raw["queries"] = list(dict.fromkeys((existing.get("queries") or []) + (p.get("queries") or [])))
            st.put_topic(core.normalise_topic({**raw, "id": tid}, existing))
        for s in sigs:
            if not st.has_signal(s["id"]):
                st.put_signal(s)
        for n in notes:
            if n.get("path") and not st.get_kv(f"note:{n['path']}"):
                # the n8n hash was a different function - keep its text so the next read diffs
                # against it (a reword stays cosmetic) instead of treating every note as new
                st.set_kv(f"note:{n['path']}", dict(n, hash=ops.content_hash(n.get("norm") or "")))
        for r in seen:                # files-night already printed / narrated exactly this content: never again
            st.set_kv(f"seen-n8n:{r['path']}#{r['hash']}", r.get("at") or core.now_iso())
        if firms:
            cur = st.get_kv("config", {}) or {}
            cur["competitors"] = list(dict.fromkeys((cur.get("competitors") or []) + firms))[:60]
            st.set_kv("config", cur)
        st.set_kv("n8n_imported", core.now_iso())
    return {"dry_run": dry_run, "topics": [{"id": k, "name": v["name"], "queries": len(v["queries"]),
                                            "depth": v.get("depth", "standard")} for k, v in plans.items()],
            "signals": len(sigs), "notes": len(notes), "competitors": firms}


#: exactly the n8n flows Vera's intelligence replaces. `notify` (the generic router)
#: and every home / estate flow stay on.
N8N_RETIRE = ("intel-night", "intel-brief", "intel-automations", "intel-command", "files-night",
              "vera-ingest-interests", "vera-deep-monitor", "vera-daily-report", "vera-report-now",
              "vera-brief-now", "vera-interest", "vera-tag", "vera-weekly-digest", "vera-podcast-overnight",
              "vera-save")


@capability("intel.n8n.retire", http_method="POST", http_path="/intel/n8n/retire", http_tags=["intel"],
            description="Switch off the n8n flows Vera's intelligence replaces (" + ", ".join(N8N_RETIRE) + "). "
                        "Deactivates only - never deletes; notify and the home/estate flows stay on. Input: dry_run "
                        "(default true), reactivate (undo: switch them back on). Output: {workflows:[{id,name,active,action}]}.")
async def cap_n8n_retire(dry_run: bool = True, reactivate: bool = False, trace_id=None):
    r = await _call("n8n.workflow.list", limit=250)
    if r.get("error"):
        return r
    rows = []
    for w in r.get("workflows") or r.get("data") or r.get("rows") or []:
        name = str(w.get("name") or "")
        if name not in N8N_RETIRE:
            continue
        want = bool(reactivate)
        if bool(w.get("active")) == want:
            act = "already active" if want else "already inactive"
        elif dry_run:
            act = "would reactivate" if want else "would deactivate"
        else:
            x = await _call("n8n.workflow.activate", id=str(w.get("id")), active=want)
            act = ("reactivated" if want else "deactivated") if not x.get("error") else f"failed: {x['error']}"
        rows.append({"id": w.get("id"), "name": name, "active": w.get("active"), "action": act})
    if not dry_run:
        store().set_kv("n8n_retired", "" if reactivate else core.now_iso())
    return {"dry_run": dry_run, "workflows": rows,
            "missing": [n for n in N8N_RETIRE if n not in {x["name"] for x in rows}]}


# ── outcomes from the product: link opens, feedback, alerts acted on ────────

@APP.get("/intel/go/alert/{snap_id}", include_in_schema=False)
async def _go_alert(snap_id: str):
    """An alert's link: counts as 'acted on', then opens the panel."""
    from fastapi.responses import RedirectResponse
    _outcome("snapshot", re.sub(r"[^a-f0-9]", "", snap_id)[:32], "alert.acted",
             questions=["intel.material_change"])
    return RedirectResponse("/intel/panel", status_code=302)


@APP.get("/intel/go/{signal_id}", include_in_schema=False)
async def _go_signal(signal_id: str):
    """A signal link in a report or a message: counts as 'opened', then redirects to the
    page the signal points at - only ever a stored http(s) URL, never one from the request."""
    from fastapi.responses import RedirectResponse
    sid = re.sub(r"[^a-f0-9]", "", signal_id)[:32]
    s = store().get_signal(sid)
    if not s or not re.match(r"(?i)^https?://", str(s.get("url") or "")):
        return HTMLResponse("<h1>No such link</h1>", status_code=404)
    _outcome("signal", sid, "link.opened", questions=["intel.signal_relevant", "intel.enrich_worth",
                                                      "intel.page_genre"])
    return RedirectResponse(s["url"], status_code=302)


@capability("intel.feedback", http_method="POST", http_path="/intel/feedback", http_tags=["intel"],
            schema=enum_schema(verdict=["useful", "not_useful", "mute_site"]),
            description="Tell intelligence whether a signal was worth it: useful | not_useful | mute_site (never "
                        "show this site again for the topic). Feeds the decision outcomes its gates learn from. "
                        "Input: signal_id (str!), verdict. Output: {ok, outcomes}.")
async def cap_feedback(signal_id: str = "", verdict: str = "useful", trace_id=None):
    st = store()
    s = st.get_signal(signal_id)
    if not s:
        return {"error": f"unknown signal: {signal_id}"}
    if verdict not in ("useful", "not_useful", "mute_site"):
        return {"error": "verdict must be useful, not_useful or mute_site"}
    n = _outcome("signal", signal_id, f"user.feedback.{verdict}")
    st.put_signal(dict(s, feedback=verdict, feedback_at=core.now_iso()))
    if verdict == "mute_site" and s.get("site"):
        t = st.get_topic(s.get("topic_id", ""))
        if t:
            st.patch_topic(t["id"], {"mute_sites": list(dict.fromkeys((t.get("mute_sites") or []) + [s["site"]]))[-60:]})
    await _event("feedback", topic=s.get("topic_id"), signal=signal_id, verdict=verdict)
    return {"ok": True, "outcomes": n}


@capability("intel.decisions.stats", http_method="GET", http_path="/intel/decisions/stats", http_tags=["intel"],
            memory="off", silent=True,
            description="How intelligence's gates are doing: per question (signal_relevant, page_genre, enrich_worth, "
                        "material_change) the decisions taken, how many got an outcome from you, and how often the "
                        "outcome says the gate was right. Input: days (default 30). Output: {questions, spend}.")
async def cap_decisions_stats(days: int = 30, trace_id=None):
    rows = store().decisions(since=_since(24 * max(1, int(days))), limit=20000)
    st = store()
    spend = {}
    for i in range(7):
        day = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d")
        rec = st.get_kv(f"spend:{day}")
        if rec:
            spend[day] = rec
    return {"questions": ledger.agreement(rows), "decisions": len(rows), "spend": spend}


@capability("intel.decisions.export", http_method="GET", http_path="/intel/decisions/export", http_tags=["intel"],
            memory="off", silent=True,
            description="intel's decisions and outcomes in the four-minds Decision v0 / Outcome v0 shapes, for "
                        "decide.ledger.import. Pointers and hashes only - no page text, no notes. Input: since (ISO), "
                        "limit. Output: {decisions, outcomes}.")
async def cap_decisions_export(since: str = "", limit: int = 5000, trace_id=None):
    rows = store().decisions(since=since, limit=max(1, min(50000, int(limit))), with_outcomes=False)
    return {"decisions": rows, "outcomes": store().outcomes_since(since, limit=max(1, min(200000, int(limit) * 4))),
            "schema": {"decision": ledger.DECISION_SCHEMA, "outcome": ledger.OUTCOME_SCHEMA}}


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
             "intel.report.build", "intel.report.list", "intel.deliver", "intel.refresh", "intel.command",
             "intel.save", "intel.session.status", "intel.notes.list", "intel.advise"],
    mode="tab", tab_order=58,
)
