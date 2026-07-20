"""
knowledgebase.py — Vera Data Fabric — Structured Knowledgebases (3rd order)
===========================================================================
Turns discovery output (pages, entities, relations, stitched tables,
collections) into a *wiki-like, structured, queryable knowledgebase* per
subject. Where `fabric.synthesize.topic` produces one lossy distilled model,
a knowledgebase is an accumulating body of knowledge:

  • ARTICLES  — LLM-written wiki pages (overview / per-entity / per-topic /
                per-table), each grounded in crawled records and carrying its
                sources. Re-running a build EXTENDS the KB (upsert by slug),
                so successive crawls grow one coherent knowledgebase a part
                at a time.
  • FACTS     — subject–predicate–object triples harvested from the entity
                graph and from article writing, deduplicated, each with a
                source. This is the queryable layer.
  • TABLES    — the stitched/structured datasets contributing to the KB stay
                first-class: kb.query searches their rows too.

Capabilities
────────────
  fabric.kb.build     Build/extend a knowledgebase from a dataset/crawl
  fabric.kb.list      List knowledgebases
  fabric.kb.get       One KB's metadata + article index
  fabric.kb.article   Full article (markdown) + its facts
  fabric.kb.query     Query a KB like an API: facts + articles + table rows,
                      optional LLM-composed answer with citations
  fabric.kb.render    Wiki-style markdown (index or article) for direct UI display
  fabric.kb.delete    Remove a knowledgebase

Loading: listed in `_module_files` right after fabric/discovery.py (it reuses
its LLM + storage helpers via lazy absolute imports, per the module pattern).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from Vera.vera.capability_orchestration import (
    capability, emit_event, now_iso,
)

log = logging.getLogger("vera.fabric_kb")


# ── Lazy bridges into sibling fabric modules (absolute imports required) ────
def _disc():
    import Vera.vera.fabric.discovery as d
    return d


def _sqlite_conn():
    return _disc()._sqlite_conn()


async def _llm(prompt: str, system: str = "", timeout: float = 60.0) -> str:
    return await _disc()._llm_generate(prompt, system, timeout=timeout)


def _sjson(raw):
    return _disc()._sjson(raw)


async def _call_cap(name: str, **kw):
    return await _disc()._call_cap(name, **kw)


def _get_graph():
    return _disc()._get_graph()


def _slug(s: str, fallback: str = "item") -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return (s or fallback)[:60]


# ── Storage ──────────────────────────────────────────────────────────────────
_TABLES_READY = False


def _ensure_tables():
    global _TABLES_READY
    if _TABLES_READY:
        return
    try:
        conn = _sqlite_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS fabric_kb (
                kb_id         TEXT PRIMARY KEY,
                subject       TEXT,
                description   TEXT,
                goal          TEXT,
                datasets      TEXT,    -- JSON list of contributing dataset ids
                status        TEXT,    -- building|ready|error
                article_count INTEGER DEFAULT 0,
                fact_count    INTEGER DEFAULT 0,
                created_at    TEXT,
                updated_at    TEXT
            );
            CREATE TABLE IF NOT EXISTS fabric_kb_articles (
                id          TEXT PRIMARY KEY,   -- kba_<sha1(kb|slug)>
                kb_id       TEXT,
                slug        TEXT,
                title       TEXT,
                kind        TEXT,               -- overview|entity|topic|table
                entity      TEXT,
                summary     TEXT,
                content_md  TEXT,
                sources     TEXT,               -- JSON list (urls / dataset ids)
                updated_at  TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_kba_kb ON fabric_kb_articles(kb_id);
            CREATE TABLE IF NOT EXISTS fabric_kb_facts (
                id           TEXT PRIMARY KEY,  -- kbf_<sha1(kb|s|p|o)>
                kb_id        TEXT,
                subject      TEXT,
                predicate    TEXT,
                object       TEXT,
                article_slug TEXT,
                source       TEXT,
                confidence   REAL DEFAULT 0.7,
                created_at   TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_kbf_kb  ON fabric_kb_facts(kb_id);
            CREATE INDEX IF NOT EXISTS idx_kbf_sub ON fabric_kb_facts(kb_id, subject);
        """)
        conn.commit()
        _TABLES_READY = True
    except Exception as e:
        log.debug("kb tables: %s", e)


def _kb_load(kb_id: str) -> Optional[Dict]:
    _ensure_tables()
    try:
        row = _sqlite_conn().execute(
            "SELECT * FROM fabric_kb WHERE kb_id=?", (kb_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["datasets"] = json.loads(d.get("datasets") or "[]")
        except Exception:
            d["datasets"] = []
        return d
    except Exception as e:
        log.debug("kb load: %s", e)
        return None


def _kb_resolve(kb_id: str = "", subject: str = "") -> Optional[Dict]:
    """Find a KB by exact id, else by subject (exact slug, else LIKE)."""
    _ensure_tables()
    if kb_id:
        kb = _kb_load(kb_id)
        if kb:
            return kb
    if subject:
        kb = _kb_load("kb_" + _slug(subject))
        if kb:
            return kb
        try:
            row = _sqlite_conn().execute(
                "SELECT kb_id FROM fabric_kb WHERE subject LIKE ? "
                "ORDER BY updated_at DESC LIMIT 1", (f"%{subject}%",)).fetchone()
            if row:
                return _kb_load(row["kb_id"])
        except Exception:
            pass
    return None


def _kb_save(kb: Dict):
    _ensure_tables()
    try:
        conn = _sqlite_conn()
        conn.execute(
            "INSERT OR REPLACE INTO fabric_kb "
            "(kb_id, subject, description, goal, datasets, status, "
            " article_count, fact_count, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (kb["kb_id"], kb.get("subject", ""), kb.get("description", ""),
             kb.get("goal", ""), json.dumps(kb.get("datasets", [])),
             kb.get("status", "ready"), int(kb.get("article_count", 0)),
             int(kb.get("fact_count", 0)),
             kb.get("created_at") or now_iso(), now_iso()),
        )
        conn.commit()
    except Exception as e:
        log.debug("kb save: %s", e)


def _article_save(kb_id: str, art: Dict):
    _ensure_tables()
    aid = "kba_" + hashlib.sha1(f"{kb_id}|{art['slug']}".encode()).hexdigest()[:18]
    try:
        conn = _sqlite_conn()
        # merge sources with any existing version of the article
        old = conn.execute(
            "SELECT sources FROM fabric_kb_articles WHERE id=?", (aid,)).fetchone()
        sources = list(art.get("sources") or [])
        if old and old["sources"]:
            try:
                for s in json.loads(old["sources"]):
                    if s not in sources:
                        sources.append(s)
            except Exception:
                pass
        conn.execute(
            "INSERT OR REPLACE INTO fabric_kb_articles "
            "(id, kb_id, slug, title, kind, entity, summary, content_md, "
            " sources, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (aid, kb_id, art["slug"], art.get("title", art["slug"]),
             art.get("kind", "topic"), art.get("entity", ""),
             art.get("summary", ""), art.get("content_md", ""),
             json.dumps(sources[:40]), now_iso()),
        )
        conn.commit()
    except Exception as e:
        log.debug("kb article save: %s", e)


def _fact_save(kb_id: str, s: str, p: str, o: str,
               article_slug: str = "", source: str = "", confidence: float = 0.7):
    s, p, o = (str(s or "").strip(), str(p or "").strip(), str(o or "").strip())
    if not s or not p or not o or len(s) > 200 or len(o) > 500:
        return
    _ensure_tables()
    fid = "kbf_" + hashlib.sha1(
        f"{kb_id}|{s.lower()}|{p.lower()}|{o.lower()}".encode()).hexdigest()[:20]
    try:
        conn = _sqlite_conn()
        conn.execute(
            "INSERT OR IGNORE INTO fabric_kb_facts "
            "(id, kb_id, subject, predicate, object, article_slug, source, "
            " confidence, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (fid, kb_id, s[:200], p[:80], o[:500], article_slug, source[:300],
             float(confidence), now_iso()),
        )
        conn.commit()
    except Exception as e:
        log.debug("kb fact save: %s", e)


def _kb_counts(kb_id: str) -> Tuple[int, int]:
    try:
        conn = _sqlite_conn()
        a = conn.execute("SELECT COUNT(*) AS n FROM fabric_kb_articles WHERE kb_id=?",
                         (kb_id,)).fetchone()["n"]
        f = conn.execute("SELECT COUNT(*) AS n FROM fabric_kb_facts WHERE kb_id=?",
                         (kb_id,)).fetchone()["n"]
        return int(a), int(f)
    except Exception:
        return 0, 0


async def _kb_emit(stage: str, **kw):
    try:
        await emit_event({"type": "fabric.kb.progress", "stage": stage, **kw})
    except Exception:
        pass


# ── Material gathering ───────────────────────────────────────────────────────
def _gather_tables(dataset_id: str) -> List[Dict]:
    """Sub-tables for the dataset, stitched ones first (they're the coherent
    view; raw fragments only matter when nothing was stitched)."""
    try:
        rows = _sqlite_conn().execute(
            "SELECT sub_dataset, title, kind, columns, row_count FROM fabric_subtables "
            "WHERE parent_dataset=? ORDER BY "
            "CASE WHEN kind='stitched' THEN 0 ELSE 1 END, row_count DESC LIMIT 40",
            (dataset_id,)).fetchall()
    except Exception:
        return []
    out, seen_stitched = [], False
    for r in rows:
        d = dict(r)
        try:
            d["columns"] = json.loads(d.get("columns") or "[]")
        except Exception:
            d["columns"] = []
        if d.get("kind") == "stitched":
            seen_stitched = True
        out.append(d)
    if seen_stitched:
        # keep stitched + the non-table kinds (api_endpoints etc.); drop raw
        # 'table' fragments that the stitched datasets already subsume
        out = [t for t in out if t.get("kind") != "table"]
    return out


def _gather_pages(dataset_id: str, limit: int = 30) -> List[Dict]:
    try:
        rows = _sqlite_conn().execute(
            "SELECT url, title, full_text, word_count FROM fabric_pages "
            "WHERE dataset_id=? ORDER BY word_count DESC LIMIT ?",
            (dataset_id, limit)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


async def _gather_entities(dataset_id: str, limit: int = 400) -> Tuple[List[Dict], List[Dict]]:
    """(entities, relations) from the 2nd-order entity graph."""
    ents, rels = [], []
    try:
        snap = await _call_cap("fabric.entity_graph.snapshot",
                               dataset_id=dataset_id, limit=limit)
        if not isinstance(snap, dict):
            return ents, rels
        id2name = {}
        for n in snap.get("nodes", []) or []:
            props = n.get("props") or {}
            name = (n.get("name") or props.get("name") or props.get("title")
                    or n.get("label") or n.get("id") or "")
            typ = n.get("type") or (n.get("labels") or ["Entity"])[0]
            if typ in ("Dataset", "FabricRecord", "Page"):
                continue
            id2name[n.get("id")] = name
            ents.append({"name": str(name)[:120],
                         "type": str(props.get("type") or typ)[:40],
                         "mentions": int(props.get("mention_count") or 1)})
        for e in snap.get("edges", []) or []:
            f = id2name.get(e.get("from") or e.get("from_id"))
            t = id2name.get(e.get("to") or e.get("to_id"))
            r = e.get("rel") or e.get("relation") or "RELATED"
            if f and t and r not in ("HAS_ENTITY", "MENTIONED_IN"):
                rels.append({"from": f, "to": t, "rel": str(r)[:60]})
    except Exception as e:
        log.debug("kb gather entities: %s", e)
    ents.sort(key=lambda x: -x["mentions"])
    return ents, rels


def _records_mentioning(dataset_id: str, term: str, limit: int = 6) -> List[Dict]:
    """Record snippets whose text mentions the term (grounding). Title/url live
    inside the data JSON — fabric_records has no such columns."""
    out = []
    if not term:
        return out
    try:
        rows = _sqlite_conn().execute(
            "SELECT text, data FROM fabric_records "
            "WHERE dataset_id=? AND text LIKE ? LIMIT ?",
            (dataset_id, f"%{term}%", limit)).fetchall()
        for r in rows:
            try:
                d = json.loads(r["data"]) if r["data"] else {}
            except Exception:
                d = {}
            if not isinstance(d, dict):
                d = {}
            out.append({"title": str(d.get("title") or d.get("name") or "")[:120],
                        "url": str(d.get("url") or d.get("link") or "")[:300],
                        "text": (r["text"] or "")[:500]})
    except Exception as e:
        log.debug("kb records mentioning: %s", e)
    return out


def _table_rows_matching(sub_dataset: str, term: str, limit: int = 5) -> List[Dict]:
    out = []
    try:
        rows = _sqlite_conn().execute(
            "SELECT data FROM fabric_records WHERE dataset_id=? AND text LIKE ? LIMIT ?",
            (sub_dataset, f"%{term}%", limit)).fetchall()
        for r in rows:
            try:
                d = json.loads(r["data"]) if r["data"] else {}
                if isinstance(d, dict):
                    out.append({k: v for k, v in d.items()
                                if not str(k).startswith("_") and k != "text"})
            except Exception:
                continue
    except Exception:
        pass
    return out


# ── Build ────────────────────────────────────────────────────────────────────
_BUILDING: Set[str] = set()   # kb_ids with a build in flight (no duplicates)


async def _plan_articles(subject: str, goal: str, existing: List[str],
                         ents: List[Dict], tables: List[Dict],
                         pages: List[Dict], max_articles: int) -> List[Dict]:
    sys = ("You are planning a structured wiki-style knowledgebase. Given a "
           "subject and the material available (entities, tables, pages), plan "
           "the set of articles that best MAPS THE FACTS of the subject. "
           "Prefer one overview plus focused articles for the most important "
           "entities/subtopics/tables. Do not duplicate existing articles "
           "unless new material clearly extends them. Output strict JSON only.")
    pr = json.dumps({
        "subject": subject,
        "goal": goal or "build a complete factual reference for the subject",
        "existing_article_slugs": existing[:60],
        "top_entities": [{"name": e["name"], "type": e["type"],
                          "mentions": e["mentions"]} for e in ents[:60]],
        "tables": [{"title": t.get("title", ""), "kind": t.get("kind", ""),
                    "columns": (t.get("columns") or [])[:10],
                    "rows": t.get("row_count", 0)} for t in tables[:20]],
        "page_titles": [p.get("title", "")[:100] for p in pages[:30]],
        "max_articles": max_articles,
    }) + ('\n\nReturn ONLY {"articles": [{"slug": "snake_case", "title": "...", '
          '"kind": "overview|entity|topic|table", "entity": "entity name or empty", '
          '"focus": "one sentence on what this article must cover"}]}.')
    d = _sjson(await _llm(pr, sys, timeout=90)) or {}
    arts = d.get("articles") if isinstance(d, dict) else None
    out = []
    if isinstance(arts, list):
        for a in arts[:max_articles]:
            if isinstance(a, dict) and a.get("slug"):
                a["slug"] = _slug(a["slug"])
                out.append(a)
    if not any(a.get("kind") == "overview" for a in out):
        out.insert(0, {"slug": "overview", "title": subject.title(),
                       "kind": "overview", "entity": "",
                       "focus": f"overview of {subject}"})
    return out


async def _write_article(kb: Dict, plan: Dict, dataset_id: str,
                         ents: List[Dict], rels: List[Dict],
                         tables: List[Dict], pages: List[Dict],
                         prev_content: str = "") -> Optional[Dict]:
    subject = kb.get("subject", "")
    term = plan.get("entity") or plan.get("title") or ""
    # Grounding context
    grounding: Dict[str, Any] = {"focus": plan.get("focus", "")}
    if plan.get("kind") == "overview":
        grounding["entities"] = [e["name"] for e in ents[:40]]
        grounding["tables"] = [{"title": t.get("title"), "rows": t.get("row_count"),
                                "columns": (t.get("columns") or [])[:10]}
                               for t in tables[:12]]
        grounding["pages"] = [{"title": p.get("title", "")[:100],
                               "excerpt": (p.get("full_text") or "")[:400]}
                              for p in pages[:8]]
    else:
        grounding["relations"] = [f"{r['from']} {r['rel']} {r['to']}"
                                  for r in rels
                                  if term.lower() in (r["from"].lower(), r["to"].lower())][:30]
        grounding["records"] = _records_mentioning(dataset_id, term, limit=6)
        trows = []
        for t in tables[:10]:
            m = _table_rows_matching(t.get("sub_dataset", ""), term, limit=4)
            if m:
                trows.append({"table": t.get("title", ""), "rows": m})
        grounding["table_rows"] = trows[:6]
    sys = ("You write one wiki article for a knowledgebase. Ground every claim "
           "in the provided material — do not invent specifics that are not "
           "supported. Write clear factual markdown with headings, and extract "
           "the atomic facts as subject-predicate-object triples. If previous "
           "content is given, MERGE it: keep its facts, integrate the new "
           "material, never lose information. Output strict JSON only.")
    pr = (json.dumps({
            "knowledgebase_subject": subject,
            "article": {"title": plan.get("title", ""), "kind": plan.get("kind", ""),
                        "entity": plan.get("entity", ""), "focus": plan.get("focus", "")},
            "material": grounding,
            "previous_content_md": (prev_content or "")[:4000],
          })[:14000]
          + '\n\nReturn ONLY {"summary": "2-3 sentences", "content_md": '
            '"the full markdown article", "facts": [{"subject": "...", '
            '"predicate": "...", "object": "..."}]}.')
    d = _sjson(await _llm(pr, sys, timeout=150)) or {}
    if not isinstance(d, dict) or not d.get("content_md"):
        return None
    srcs = [r.get("url") for r in (grounding.get("records") or []) if r.get("url")]
    srcs += [dataset_id]
    return {
        "slug": plan["slug"], "title": plan.get("title") or plan["slug"],
        "kind": plan.get("kind", "topic"), "entity": plan.get("entity", ""),
        "summary": str(d.get("summary", ""))[:600],
        "content_md": str(d.get("content_md", ""))[:40000],
        "sources": srcs,
        "facts": [f for f in (d.get("facts") or []) if isinstance(f, dict)][:60],
    }


async def _mirror_kb_graph(kb: Dict, article_slugs: List[str]):
    try:
        graph = _get_graph()
        if not (graph and graph.available):
            return
        kb_id = kb["kb_id"]
        await graph.upsert_node("Knowledgebase", kb_id,
                                {"id": kb_id, "name": kb.get("subject", "")[:120],
                                 "articles": kb.get("article_count", 0),
                                 "facts": kb.get("fact_count", 0)})
        for ds in (kb.get("datasets") or [])[:20]:
            await graph.upsert_node("Dataset", ds, {"id": ds})
            await graph.link("Knowledgebase", kb_id, "Dataset", ds, rel="BUILT_FROM")
        for slug in article_slugs[:60]:
            aid = f"{kb_id}:{slug}"
            await graph.upsert_node("Article", aid, {"id": aid, "name": slug})
            await graph.link("Knowledgebase", kb_id, "Article", aid, rel="HAS_ARTICLE")
    except Exception as e:
        log.debug("kb graph mirror: %s", e)


@capability(
    "fabric.kb.build",
    http_method="POST", http_path="/fabric/kb/build", http_tags=["fabric", "kb"],
    memory="on", streams=["fabric.kb.progress"],
    description="Build or EXTEND a structured knowledgebase (wiki) for a subject "
                "from discovery output: entities+relations, stitched tables, and "
                "pages of a dataset/crawl. Plans articles, writes them grounded in "
                "the material, and harvests subject-predicate-object facts. "
                "Re-running with new datasets grows the same KB incrementally. "
                "Input: subject (str — defaults from the dataset's crawl topic), "
                "dataset_id (str), crawl_id (str), kb_id (str — extend an existing "
                "KB), goal (str), max_articles (int=12), "
                "rebuild (bool=False — rewrite existing articles too). "
                "Output: {kb_id, subject, article_count, fact_count, articles}.",
)
async def cap_kb_build(
    subject: str = "",
    dataset_id: str = "",
    crawl_id: str = "",
    kb_id: str = "",
    goal: str = "",
    max_articles: int = 12,
    rebuild: bool = False,
    trace_id=None,
) -> Dict:
    disc = _disc()
    # Extending an existing KB by id: adopt its subject (and default dataset).
    if kb_id and not subject:
        _ex = _kb_load(kb_id)
        if _ex:
            subject = _ex.get("subject", "")
            if not dataset_id and _ex.get("datasets"):
                dataset_id = _ex["datasets"][-1]
    # Resolve dataset/subject from the crawl when only one is given.
    if crawl_id and not dataset_id:
        fr = disc._load_frontier(crawl_id)
        if fr:
            dataset_id = fr.get("dataset_id", "")
            if not subject:
                try:
                    subject = (json.loads(fr.get("config") or "{}") or {}).get("topic", "")
                except Exception:
                    pass
    if not subject:
        subject = re.sub(r"^(web|topic)\.", "", dataset_id or "").replace("_", " ").strip()
    if not subject:
        return {"error": "subject required (or a dataset/crawl to derive it from)"}
    if not dataset_id:
        return {"error": "dataset_id or crawl_id required"}

    kb_id = "kb_" + _slug(subject)
    if kb_id in _BUILDING:
        return {"error": f"kb {kb_id} build already running", "kb_id": kb_id}
    _BUILDING.add(kb_id)
    try:
        _ensure_tables()
        kb = _kb_load(kb_id) or {
            "kb_id": kb_id, "subject": subject, "description": "", "goal": goal,
            "datasets": [], "status": "building", "article_count": 0,
            "fact_count": 0, "created_at": now_iso(),
        }
        if goal:
            kb["goal"] = goal
        if dataset_id and dataset_id not in kb["datasets"]:
            kb["datasets"].append(dataset_id)
        kb["status"] = "building"
        _kb_save(kb)
        await _kb_emit("gathering", kb_id=kb_id, subject=subject,
                       dataset_id=dataset_id,
                       message=f"gathering material for '{subject}'…")

        ents, rels = await _gather_entities(dataset_id)
        tables = _gather_tables(dataset_id)
        pages = _gather_pages(dataset_id)

        conn = _sqlite_conn()
        existing_rows = conn.execute(
            "SELECT slug, content_md FROM fabric_kb_articles WHERE kb_id=?",
            (kb_id,)).fetchall()
        existing = {r["slug"]: r["content_md"] for r in existing_rows}

        await _kb_emit("planning", kb_id=kb_id,
                       entities=len(ents), tables=len(tables), pages=len(pages),
                       message=f"planning articles ({len(ents)} entities, "
                               f"{len(tables)} tables, {len(pages)} pages)…")
        plans = await _plan_articles(subject, kb.get("goal", ""),
                                     sorted(existing.keys()), ents, tables,
                                     pages, max(1, min(int(max_articles), 40)))
        written: List[str] = []
        sem = asyncio.Semaphore(2)

        async def _one(plan):
            async with sem:
                prev = existing.get(plan["slug"], "") if not rebuild else ""
                try:
                    art = await _write_article(kb, plan, dataset_id, ents, rels,
                                               tables, pages, prev_content=prev)
                except Exception as e:
                    log.warning("kb article %s: %s", plan.get("slug"), e)
                    return
                if not art:
                    return
                _article_save(kb_id, art)
                for f in art.get("facts", []):
                    _fact_save(kb_id, f.get("subject", ""), f.get("predicate", ""),
                               f.get("object", ""), article_slug=art["slug"],
                               source=dataset_id)
                written.append(art["slug"])
                await _kb_emit("article", kb_id=kb_id, slug=art["slug"],
                               title=art["title"],
                               message=f"wrote article: {art['title']}")

        await asyncio.gather(*[_one(p) for p in plans], return_exceptions=True)

        # Mechanical facts straight from the relation graph (no LLM, cheap).
        for r in rels[:800]:
            _fact_save(kb_id, r["from"], r["rel"], r["to"], source=dataset_id,
                       confidence=0.6)

        a_n, f_n = _kb_counts(kb_id)
        kb.update({"status": "ready", "article_count": a_n, "fact_count": f_n})
        if not kb.get("description"):
            ov = _sqlite_conn().execute(
                "SELECT summary FROM fabric_kb_articles WHERE kb_id=? AND slug='overview'",
                (kb_id,)).fetchone()
            if ov:
                kb["description"] = (ov["summary"] or "")[:500]
        _kb_save(kb)
        await _mirror_kb_graph(kb, written)
        await _kb_emit("done", kb_id=kb_id, articles=a_n, facts=f_n,
                       written=len(written),
                       message=f"knowledgebase '{subject}': {a_n} articles, "
                               f"{f_n} facts ({len(written)} written this pass)")
        return {"kb_id": kb_id, "subject": subject, "article_count": a_n,
                "fact_count": f_n, "articles": written}
    except Exception as e:
        log.warning("kb build %s: %s", kb_id, e)
        try:
            kb = _kb_load(kb_id)
            if kb:
                kb["status"] = "error"
                _kb_save(kb)
        except Exception:
            pass
        return {"error": str(e), "kb_id": kb_id}
    finally:
        _BUILDING.discard(kb_id)


# ── Read / query ─────────────────────────────────────────────────────────────
@capability(
    "fabric.kb.list",
    http_method="GET", http_path="/fabric/kb/list", http_tags=["fabric", "kb"],
    memory="off", silent=True,
    description="List knowledgebases. Output: {knowledgebases:[{kb_id,subject,"
                "description,article_count,fact_count,status,updated_at}]}.",
)
async def cap_kb_list(trace_id=None) -> Dict:
    _ensure_tables()
    try:
        rows = _sqlite_conn().execute(
            "SELECT kb_id, subject, description, status, article_count, "
            "fact_count, updated_at FROM fabric_kb ORDER BY updated_at DESC "
            "LIMIT 200").fetchall()
        return {"knowledgebases": [dict(r) for r in rows]}
    except Exception as e:
        return {"knowledgebases": [], "error": str(e)}


@capability(
    "fabric.kb.get",
    http_method="GET", http_path="/fabric/kb/get", http_tags=["fabric", "kb"],
    memory="off", silent=True,
    description="One knowledgebase's metadata + article index. "
                "Input: kb_id (str) or subject (str). "
                "Output: {kb, articles:[{slug,title,kind,entity,summary}]}.",
)
async def cap_kb_get(kb_id: str = "", subject: str = "", trace_id=None) -> Dict:
    kb = _kb_resolve(kb_id, subject)
    if not kb:
        return {"error": "knowledgebase not found"}
    try:
        rows = _sqlite_conn().execute(
            "SELECT slug, title, kind, entity, summary, updated_at "
            "FROM fabric_kb_articles WHERE kb_id=? "
            "ORDER BY CASE WHEN slug='overview' THEN 0 ELSE 1 END, title",
            (kb["kb_id"],)).fetchall()
        return {"kb": kb, "articles": [dict(r) for r in rows]}
    except Exception as e:
        return {"kb": kb, "articles": [], "error": str(e)}


@capability(
    "fabric.kb.article",
    http_method="GET", http_path="/fabric/kb/article", http_tags=["fabric", "kb"],
    memory="off", silent=True,
    description="Full knowledgebase article (markdown) + its facts. "
                "Input: kb_id (str) or subject (str), slug (str!). "
                "Output: {article:{...,content_md}, facts:[...]}.",
)
async def cap_kb_article(kb_id: str = "", subject: str = "", slug: str = "",
                         trace_id=None) -> Dict:
    kb = _kb_resolve(kb_id, subject)
    if not kb:
        return {"error": "knowledgebase not found"}
    if not slug:
        return {"error": "slug required"}
    try:
        conn = _sqlite_conn()
        row = conn.execute(
            "SELECT * FROM fabric_kb_articles WHERE kb_id=? AND slug=?",
            (kb["kb_id"], slug)).fetchone()
        if not row:
            return {"error": f"article '{slug}' not found"}
        art = dict(row)
        try:
            art["sources"] = json.loads(art.get("sources") or "[]")
        except Exception:
            art["sources"] = []
        facts = conn.execute(
            "SELECT subject, predicate, object, source, confidence "
            "FROM fabric_kb_facts WHERE kb_id=? AND article_slug=? LIMIT 200",
            (kb["kb_id"], slug)).fetchall()
        return {"article": art, "facts": [dict(f) for f in facts]}
    except Exception as e:
        return {"error": str(e)}


@capability(
    "fabric.kb.query",
    http_method="POST", http_path="/fabric/kb/query", http_tags=["fabric", "kb"],
    memory="on",
    description="Query a knowledgebase like an API. Searches facts (s-p-o triples), "
                "articles, and the KB's structured table rows; mode 'answer' (or a "
                "question ending in '?') also composes an LLM answer with citations. "
                "Input: query (str!), kb_id (str) or subject (str), "
                "mode (auto|facts|articles|rows|answer = auto), limit (int=12). "
                "Output: {facts, articles, rows, answer?, kb_id}.",
)
async def cap_kb_query(
    query: str = "",
    kb_id: str = "",
    subject: str = "",
    mode: str = "auto",
    limit: int = 12,
    trace_id=None,
) -> Dict:
    if not query.strip():
        return {"error": "query required"}
    kb = _kb_resolve(kb_id, subject)
    if not kb:
        return {"error": "knowledgebase not found — see fabric.kb.list"}
    kbid = kb["kb_id"]
    q = query.strip()
    like = f"%{q}%"
    limit = max(1, min(int(limit or 12), 50))
    conn = _sqlite_conn()
    out: Dict[str, Any] = {"kb_id": kbid, "subject": kb.get("subject", ""),
                           "query": q, "facts": [], "articles": [], "rows": []}

    # Terms for broader matching (full phrase first, then significant words)
    words = [w for w in re.findall(r"[a-zA-Z0-9]{3,}", q)][:6]

    def _fact_search(term):
        try:
            t = f"%{term}%"
            return conn.execute(
                "SELECT subject, predicate, object, article_slug, source, confidence "
                "FROM fabric_kb_facts WHERE kb_id=? AND "
                "(subject LIKE ? OR predicate LIKE ? OR object LIKE ?) LIMIT ?",
                (kbid, t, t, t, limit)).fetchall()
        except Exception:
            return []

    if mode in ("auto", "facts", "answer"):
        rows = _fact_search(q)
        if not rows and words:
            seen = set()
            rows = []
            for w in words:
                for r in _fact_search(w):
                    k = (r["subject"], r["predicate"], r["object"])
                    if k not in seen:
                        seen.add(k)
                        rows.append(r)
                if len(rows) >= limit:
                    break
        out["facts"] = [dict(r) for r in rows[:limit]]

    if mode in ("auto", "articles", "answer"):
        try:
            rows = conn.execute(
                "SELECT slug, title, kind, entity, summary "
                "FROM fabric_kb_articles WHERE kb_id=? AND "
                "(title LIKE ? OR summary LIKE ? OR content_md LIKE ?) LIMIT ?",
                (kbid, like, like, like, limit)).fetchall()
            out["articles"] = [dict(r) for r in rows]
        except Exception:
            pass

    if mode in ("auto", "rows", "answer"):
        for ds in (kb.get("datasets") or [])[:6]:
            for t in _gather_tables(ds)[:8]:
                m = _table_rows_matching(t.get("sub_dataset", ""), q, limit=4)
                for r in m:
                    out["rows"].append({"table": t.get("title", ""), "row": r})
                if len(out["rows"]) >= limit:
                    break
            if len(out["rows"]) >= limit:
                break

    want_answer = mode == "answer" or (mode == "auto" and q.rstrip().endswith("?"))
    if want_answer:
        # Compose an answer grounded ONLY in what the KB returned.
        art_bodies = []
        for a in out["articles"][:3]:
            try:
                row = conn.execute(
                    "SELECT content_md FROM fabric_kb_articles WHERE kb_id=? AND slug=?",
                    (kbid, a["slug"])).fetchone()
                if row:
                    art_bodies.append({"slug": a["slug"],
                                       "content": (row["content_md"] or "")[:3000]})
            except Exception:
                pass
        sys = ("Answer the question using ONLY the provided knowledgebase "
               "material. Cite article slugs / table names inline like "
               "[overview] or [table:name]. If the material doesn't contain "
               "the answer, say so plainly.")
        pr = json.dumps({"question": q,
                         "facts": out["facts"][:25],
                         "articles": art_bodies,
                         "table_rows": out["rows"][:15]})[:13000]
        try:
            ans = await _llm(pr, sys, timeout=90)
            if ans:
                out["answer"] = ans.strip()[:4000]
        except Exception as e:
            log.debug("kb answer: %s", e)
    return out


@capability(
    "fabric.kb.render",
    http_method="GET", http_path="/fabric/kb/render", http_tags=["fabric", "kb"],
    memory="off", silent=True,
    description="Render a knowledgebase as consumable wiki markdown: the index "
                "page (no slug) or one article (slug). Intended for direct display "
                "in UI panels/drawers that render markdown. "
                "Input: kb_id (str) or subject (str), slug (str). "
                "Output: {markdown, title, render:'markdown'}.",
)
async def cap_kb_render(kb_id: str = "", subject: str = "", slug: str = "",
                        trace_id=None) -> Dict:
    kb = _kb_resolve(kb_id, subject)
    if not kb:
        return {"error": "knowledgebase not found"}
    conn = _sqlite_conn()
    if slug:
        row = conn.execute(
            "SELECT * FROM fabric_kb_articles WHERE kb_id=? AND slug=?",
            (kb["kb_id"], slug)).fetchone()
        if not row:
            return {"error": f"article '{slug}' not found"}
        art = dict(row)
        md = f"# {art.get('title') or slug}\n\n{art.get('content_md') or ''}"
        try:
            sources = json.loads(art.get("sources") or "[]")
        except Exception:
            sources = []
        if sources:
            md += "\n\n---\n**Sources:** " + ", ".join(str(s) for s in sources[:12])
        return {"markdown": md, "title": art.get("title") or slug,
                "render": "markdown"}
    # index page
    rows = conn.execute(
        "SELECT slug, title, kind, summary FROM fabric_kb_articles WHERE kb_id=? "
        "ORDER BY CASE WHEN slug='overview' THEN 0 ELSE 1 END, title",
        (kb["kb_id"],)).fetchall()
    md = [f"# {kb.get('subject','Knowledgebase').title()}",
          "",
          kb.get("description", ""),
          "",
          f"*{kb.get('article_count',0)} articles - {kb.get('fact_count',0)} facts - "
          f"built from {len(kb.get('datasets') or [])} dataset(s)*",
          "",
          "## Articles"]
    for r in rows:
        d = dict(r)
        md.append(f"- **{d.get('title') or d['slug']}** ({d.get('kind','topic')}) - "
                  f"{(d.get('summary') or '').strip()[:180]}")
    return {"markdown": "\n".join(md), "title": kb.get("subject", ""),
            "render": "markdown"}


@capability(
    "fabric.kb.delete",
    http_method="POST", http_path="/fabric/kb/delete", http_tags=["fabric", "kb"],
    memory="off",
    description="Delete a knowledgebase (its articles + facts; contributing "
                "datasets are untouched). Input: kb_id (str!).",
)
async def cap_kb_delete(kb_id: str = "", trace_id=None) -> Dict:
    if not kb_id:
        return {"error": "kb_id required"}
    _ensure_tables()
    try:
        conn = _sqlite_conn()
        conn.execute("DELETE FROM fabric_kb_facts WHERE kb_id=?", (kb_id,))
        conn.execute("DELETE FROM fabric_kb_articles WHERE kb_id=?", (kb_id,))
        conn.execute("DELETE FROM fabric_kb WHERE kb_id=?", (kb_id,))
        conn.commit()
        return {"ok": True, "kb_id": kb_id}
    except Exception as e:
        return {"error": str(e)}


# ── Graph node actions: make KBs reachable from Dataset nodes ────────────────
def _register_kb_node_actions():
    try:
        from Vera.vera.fabric.data_fabric import _NODE_ACTION_REGISTRY
    except Exception as e:
        log.debug("kb node-action registry unavailable: %s", e)
        return
    ds_actions = _NODE_ACTION_REGISTRY.setdefault("Dataset", [])
    have = {a.get("id") for a in ds_actions}
    if "stitch_subtables" not in have:
        ds_actions.append({
            "id": "stitch_subtables", "label": "Stitch subtables", "icon": "⧉",
            "capability": "fabric.subtables.stitch",
            "args": {"parent_dataset": "$id"},
            "options": [{"name": "min_similarity", "type": "float", "default": 0.5,
                         "label": "Schema similarity"},
                        {"name": "use_llm", "type": "bool", "default": True,
                         "label": "LLM header alignment"}],
            "context": "Merge this dataset's scraped sub-table fragments into "
                       "single coherent tables (schema-matched, deduplicated, "
                       "with per-row provenance).",
        })
    if "build_kb" not in have:
        ds_actions.append({
            "id": "build_kb", "label": "Build knowledgebase", "icon": "✎",
            "capability": "fabric.kb.build",
            "args": {"dataset_id": "$id"},
            "options": [{"name": "subject", "type": "str", "default": "",
                         "label": "Subject (blank = derive)"},
                        {"name": "max_articles", "type": "int", "default": 12,
                         "label": "Max articles this pass"}],
            "context": "Write/extend a structured wiki knowledgebase from this "
                       "dataset's entities, tables and pages — then query it "
                       "with fabric.kb.query.",
        })
    _NODE_ACTION_REGISTRY["Knowledgebase"] = [
        {"id": "kb_read", "label": "Read (index)", "icon": "≡",
         "capability": "fabric.kb.render", "args": {"kb_id": "$id"},
         "context": "Render the knowledgebase index in the content drawer."},
        {"id": "kb_extend", "label": "Extend from dataset", "icon": "⤵",
         "capability": "fabric.kb.build", "args": {"kb_id": "$id"},
         "options": [{"name": "dataset_id", "type": "str", "default": "",
                      "label": "Dataset to add"}]},
        {"id": "kb_delete", "label": "Delete knowledgebase", "icon": "✕",
         "capability": "fabric.kb.delete", "args": {"kb_id": "$id"},
         "confirm": "Delete this knowledgebase?"},
    ]
    log.info("fabric_kb: registered Dataset/Knowledgebase node actions")


_register_kb_node_actions()

log.info("fabric_kb: knowledgebase module loaded "
         "(caps: kb.build/list/get/article/query/render/delete)")
