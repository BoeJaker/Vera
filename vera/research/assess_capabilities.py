# -*- coding: utf-8 -*-
"""
Assessment — the scorer registry, the capabilities over it, and ranking across records (EXPLODE.md §6, track F).

A SCORER is a layer whose output is assessments rather than cards: {key, score, confidence, by, on, evidence}.
Registered scorers run over a text (prose) or an Explode contract plus its sources (code); the assessments render
as the verdict rail on the source plate and as badges on cards. On request (`persist=true`, records only) they are
written into the record's `data.assess`, which is what makes ranking across records one query (`assess.rank`).
Nothing is persisted otherwise.

Register another scorer with one call and it is in every explode and every rank:

    register_scorer("factual", "factual", "prose", fn, by="claim checker", where="node tier", default_on=False)
    fn(ctx) → [assessment, …]   ctx = {text, path, paragraphs, doc (code), texts (code), git, tests}

Capabilities
    assess.scorers   the registered scorers
    assess.prose     text | record_id (+ scorers, persist) → assessments
    assess.code      path | paths | text (+ scorers) → assessments over the code contract
    assess.rank      records ordered by a weighted composite of their persisted assessments
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

log = logging.getLogger("vera.assess")

try:
    from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY, capability
    _CAP_AVAILABLE = True
except ImportError:  # pragma: no cover
    CAPABILITY_REGISTRY = {}
    _CAP_AVAILABLE = False

    def capability(*_a, **_k):
        def deco(fn):
            return fn
        return deco


def _core():
    try:
        from Vera.vera.research import assess_core as core
    except ImportError:
        from vera.research import assess_core as core
    return core


def _explode():
    import sys
    mod = sys.modules.get("explode_capabilities")     # the loader's copy, in the app
    if mod is not None:
        return mod
    try:
        from Vera.vera.research import explode_capabilities as mod  # noqa: F811
    except ImportError:
        from vera.research import explode_capabilities as mod       # noqa: F811
    return mod


# ── the registry ──────────────────────────────────────────────────────────────────────────────────
SCORERS: Dict[str, Dict] = {}
_ORDER: List[str] = []


def register_scorer(key: str, label: str, kind: str, fn: Callable[[Dict], Awaitable[List[Dict]]], by: str = "", where: str = "host",
                    default_on: bool = True, needs: Optional[List[str]] = None):
    """kind: 'prose' | 'code'. fn is awaited with the ctx and returns assessments. A composite `needs` the keys it
    reads and runs after them."""
    if key not in SCORERS:
        _ORDER.append(key)
    SCORERS[key] = {"key": key, "label": label, "kind": kind, "fn": fn, "by": by, "where": where, "default_on": bool(default_on), "needs": list(needs or [])}
    return SCORERS[key]


def scorer_list(kind: str = "") -> List[Dict]:
    return [{k: v for k, v in SCORERS[i].items() if k != "fn"} for i in _ORDER if not kind or SCORERS[i]["kind"] == kind]


async def run_scorers(kind: str, ctx: Dict, scorers: Optional[List[str]] = None) -> Dict:
    """Run the chosen scorers of a kind in order; each one's failure is its own receipt. Returns
    {assessments, receipts}. Composites see everything scored before them through ctx['assessments']."""
    if isinstance(scorers, str):
        scorers = [s.strip() for s in scorers.split(",") if s.strip()] or None
    wanted = [k for k in _ORDER if SCORERS[k]["kind"] == kind and ((scorers is None and SCORERS[k]["default_on"]) or (scorers is not None and k in scorers))]
    # a composite runs after what it reads
    wanted.sort(key=lambda k: 1 if SCORERS[k]["needs"] else 0)
    ctx.setdefault("assessments", [])
    receipts = []
    for k in wanted:
        S = SCORERS[k]
        rec = {"id": "assess." + k, "label": S["label"], "kind": "assessment", "by": S["by"], "where": S["where"], "on": True, "count": 0, "ms": 0}
        t0 = time.monotonic()
        try:
            out = await S["fn"](ctx) or []
        except Exception as e:
            log.warning("scorer %s: %s", k, e)
            out = []
            rec["error"] = str(e)[:200]
        rec["ms"] = int((time.monotonic() - t0) * 1000)
        out = [a for a in out if a and a.get("score") is not None]
        for a in out:
            a.setdefault("key", k); a.setdefault("on", "source"); a.setdefault("evidence", [])
        ctx["assessments"].extend(out)
        rec["count"] = len(out)
        receipts.append(rec)
    return {"assessments": ctx["assessments"], "receipts": receipts}


# ── the built-in scorers ──────────────────────────────────────────────────────────────────────────
async def _s_readability(ctx):
    a = _core().readability(ctx["text"], ctx.get("path", "")); return [a] if a else []


async def _s_structure(ctx):
    a = _core().structure(ctx["text"], ctx.get("paragraphs"), ctx.get("path", "")); return [a] if a else []


async def _s_sources(ctx):
    a = _core().sources(ctx["text"], ctx.get("path", "")); return [a] if a else []


async def _s_ai(ctx):
    a = _core().ai_likelihood_stylometry(ctx["text"], ctx.get("path", "")); return [a] if a else []


async def _s_lang(ctx):
    cap = CAPABILITY_REGISTRY.get("nlp.langid")
    if not cap:
        return []
    fn = cap.get("raw") or cap.get("func")
    res = await fn(text=ctx["text"][:2000], trace_id=None)
    if not res or res.get("error"):
        raise RuntimeError((res or {}).get("error", "nlp.langid unavailable"))
    top = (res.get("langs") or [{}])[0]
    return [{"key": "lang", "label": "language · " + str(res.get("lang") or top.get("lang") or "?"), "score": round(float(top.get("score") or 0), 3),
             "confidence": round(float(top.get("score") or 0), 3), "by": "nlp.langid", "on": "source", "evidence": []}]


async def _s_trust(ctx):
    a = _core().trust(ctx.get("assessments", [])); return [a] if a else []


async def _s_complexity(ctx):
    return _core().complexity(ctx["doc"], ctx["texts"])


async def _s_smells(ctx):
    return _core().smells(ctx["doc"], ctx["texts"])


async def _s_clones(ctx):
    return _core().clones(ctx["doc"], ctx["texts"])


async def _s_tests(ctx):
    return _core().tests_reference(ctx["doc"], ctx.get("tests") or "")


async def _s_provenance(ctx):
    return _core().provenance(ctx["doc"], ctx.get("git") or {})


async def _s_health(ctx):
    a = _core().code_summary(ctx.get("assessments", [])); return [a] if a else []


register_scorer("readability", "readable", "prose", _s_readability, by="Flesch reading ease")
register_scorer("structure", "structured", "prose", _s_structure, by="paragraph shape · headings · lists")
register_scorer("sources", "sourced", "prose", _s_sources, by="links · citations · attributions")
register_scorer("ai_likelihood", "AI-generated", "prose", _s_ai, by="stylometry · heuristic (no model)")
register_scorer("lang", "language", "prose", _s_lang, by="nlp.langid", where="node tier", default_on=False)
register_scorer("trust", "trust", "prose", _s_trust, by="composite", needs=["sources", "ai_likelihood", "readability", "structure"])
register_scorer("complexity", "simple", "code", _s_complexity, by="cyclomatic")
register_scorer("smells", "no smells", "code", _s_smells, by="security patterns")
register_scorer("clones", "no clones", "code", _s_clones, by="normalised body hash")
register_scorer("tests", "tested", "code", _s_tests, by="named in tests/")
register_scorer("provenance", "under git", "code", _s_provenance, by="git log")
register_scorer("health", "health", "code", _s_health, by="composite", needs=["complexity", "smells", "clones", "tests"])


# ── the contexts: what the code scorers are given ─────────────────────────────────────────────────
def _read_test_corpus(root: str, max_files: int = 400, max_bytes: int = 200000) -> str:
    """Every test file's text, joined — what `tests` greps symbol names in. Capped; read once per call."""
    tdir = os.path.join(root, "tests")
    if not os.path.isdir(tdir):
        return ""
    parts, n = [], 0
    for name in sorted(os.listdir(tdir)):
        if not (name.startswith("test_") and name.endswith((".py", ".cjs", ".mjs", ".js"))):
            continue
        try:
            with open(os.path.join(tdir, name), "r", encoding="utf-8", errors="replace") as fh:
                parts.append(fh.read(max_bytes))
        except Exception:
            continue
        n += 1
        if n >= max_files:
            break
    return "\n".join(parts)


async def _git_facts(root: str, paths: List[str]) -> Dict[str, Dict]:
    """{path: {sha, author, date}} from `git log -1` per file, off the loop through spawn_core (never a fork on
    the loop). Files git does not know come back empty."""
    try:
        from Vera.vera.execution.spawn_core import run_argv
    except ImportError:
        try:
            from vera.execution.spawn_core import run_argv
        except ImportError:
            return {}
    out: Dict[str, Dict] = {}
    for p in paths[:40]:
        try:
            r = await run_argv(["git", "log", "-1", "--format=%H|%an|%ad", "--date=short", "--", p], timeout=20, cwd=root)
        except Exception:
            continue
        line = (r.get("stdout") or "").strip()
        if r.get("ok") and line and "|" in line:
            sha, author, date = (line.split("|") + ["", ""])[:3]
            out[p] = {"sha": sha, "author": author, "date": date}
    return out


async def assess_code_doc(doc: Dict, texts: Dict[str, str], scorers: Optional[List[str]] = None, repo_root: str = "",
                          tests: Optional[str] = None, git: Optional[Dict[str, Dict]] = None) -> Dict:
    """The code scorers over a contract and its sources. The contract's own assessments (syntax, resolved) are
    the starting point, so the health composite sees them. `tests` / `git` may be handed in; otherwise they are
    read from repo_root when one is given."""
    ctx: Dict[str, Any] = {"doc": doc, "texts": texts, "assessments": list(doc.get("assessments") or [])}
    loop = asyncio.get_running_loop()
    if tests is not None:
        ctx["tests"] = tests
    elif repo_root and (scorers is None or "tests" in scorers):
        ctx["tests"] = await loop.run_in_executor(None, _read_test_corpus, repo_root)
    if git is not None:
        ctx["git"] = git
    elif repo_root and (scorers is None or "provenance" in scorers):
        ctx["git"] = await _git_facts(repo_root, [p for p in texts if os.path.isfile(os.path.join(repo_root, p))])
    return await run_scorers("code", ctx, scorers)


async def assess_prose_text(text: str, paragraphs: Optional[List[Dict]] = None, path: str = "", scorers: Optional[List[str]] = None,
                            prior: Optional[List[Dict]] = None) -> Dict:
    ctx: Dict[str, Any] = {"text": text, "paragraphs": paragraphs, "path": path, "assessments": list(prior or [])}
    return await run_scorers("prose", ctx, scorers)


# ── persistence and ranking ───────────────────────────────────────────────────────────────────────
def _sqlite_conn():
    from Vera.vera.fabric.data_fabric import _sqlite_conn as sc
    return sc()


def persist_assessments(record_id: str, assessments: List[Dict]) -> Dict:
    """Write the source-level assessments into the record's data.assess ({key: {score, confidence, by, at}})."""
    conn = _sqlite_conn()
    try:
        row = conn.execute("SELECT data FROM fabric_records WHERE id=? LIMIT 1", (record_id,)).fetchone()
        if not row:
            return {"error": "record %s not found" % record_id}
        try:
            data = json.loads((row["data"] if hasattr(row, "keys") else row[0]) or "{}") or {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {"_": data}
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        acc = data.get("assess") if isinstance(data.get("assess"), dict) else {}
        for a in assessments:
            if a.get("on") in (None, "", "source") and a.get("score") is not None:
                acc[a["key"]] = {"score": a["score"], "confidence": a.get("confidence"), "by": a.get("by", ""), "label": a.get("label", ""), "at": now}
        data["assess"] = acc
        conn.execute("UPDATE fabric_records SET data=? WHERE id=?", (json.dumps(data), record_id))
        conn.commit()
        return {"ok": True, "record_id": record_id, "keys": sorted(acc)}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def rank_records(dataset_id: str = "", keys: Optional[List[str]] = None, weights: Optional[Dict[str, float]] = None, limit: int = 50,
                 invert: Optional[List[str]] = None) -> Dict:
    """Records ordered by a weighted composite of their persisted assessments. keys: which to read (default: every
    key present); weights: per key (default 1); invert: keys where LOWER is better (ai_likelihood). A record missing
    a key is scored on the keys it has, and says so."""
    conn = _sqlite_conn()
    try:
        q = "SELECT id, dataset_id, substr(text,1,120) AS head, data FROM fabric_records WHERE json_extract(data,'$.assess') IS NOT NULL"
        args: List[Any] = []
        if dataset_id:
            q += " AND dataset_id=?"; args.append(dataset_id)
        rows = conn.execute(q + " LIMIT 5000", args).fetchall()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    inv = set(invert or ["ai_likelihood"])
    w = dict(weights or {})
    out = []
    for r in rows:
        try:
            data = json.loads((r["data"] if hasattr(r, "keys") else r[3]) or "{}")
        except Exception:
            continue
        acc = data.get("assess") or {}
        use = [k for k in (keys or list(acc)) if k in acc and isinstance(acc[k], dict) and acc[k].get("score") is not None]
        if not use:
            continue
        num = sum(w.get(k, 1.0) * ((1 - acc[k]["score"]) if k in inv else acc[k]["score"]) for k in use)
        den = sum(w.get(k, 1.0) for k in use)
        out.append({"id": r["id"] if hasattr(r, "keys") else r[0], "dataset_id": r["dataset_id"] if hasattr(r, "keys") else r[1],
                    "head": (r["head"] if hasattr(r, "keys") else r[2]) or "", "composite": round(num / den, 3),
                    "scores": {k: acc[k]["score"] for k in use}, "missing": [k for k in (keys or []) if k not in use]})
    out.sort(key=lambda x: -x["composite"])
    return {"ok": True, "count": len(out), "records": out[:max(1, min(500, limit))], "keys": keys or [], "weights": w, "invert": sorted(inv)}


# ── capabilities ──────────────────────────────────────────────────────────────────────────────────
if _CAP_AVAILABLE:
    @capability(
        "assess.scorers", http_method="GET", http_path="/assess/scorers", http_tags=["assess"], memory="off",
        description="The registered assessment scorers — prose and code — with what produces each, where it runs and whether it is on by default. Output: {scorers:[{key, label, kind, by, where, default_on, needs}]}.",
    )
    async def cap_assess_scorers(kind: str = "", trace_id=None) -> Dict:
        return {"ok": True, "scorers": scorer_list(kind)}

    @capability(
        "assess.prose", http_method="POST", http_path="/assess/prose", http_tags=["assess", "nlp"], memory="off",
        description=("Assess prose: readability, structure, sources, AI-likelihood (stylometric, low confidence — no model), "
                     "language (node tier, off by default), and a trust composite with its weights stated. Input: text | record_id, "
                     "scorers (list — default: the ones on by default), persist (bool — records only: write the scores into "
                     "the record's data.assess so assess.rank can order records by them). Output: {ok, assessments:[{key, label, "
                     "score, confidence, by, on, evidence}], receipts, persisted?}."),
    )
    async def cap_assess_prose(text: str = "", record_id: str = "", scorers: Optional[List[str]] = None, persist: bool = False, trace_id=None) -> Dict:
        X = _explode()
        if record_id:
            rec = await asyncio.get_running_loop().run_in_executor(None, X._read_record, record_id)
            if not rec:
                return {"error": "record %s not found" % record_id}
            text = (rec.get("text") or "")[:60000]
        if not text:
            return {"error": "text or record_id required"}
        paras = X.paragraphs_of(text)
        res = await assess_prose_text(text, paras, record_id or "", scorers)
        out = {"ok": True, "assessments": res["assessments"], "receipts": res["receipts"], "record_id": record_id}
        if persist and record_id:
            out["persisted"] = await asyncio.get_running_loop().run_in_executor(None, persist_assessments, record_id, res["assessments"])
        return out

    @capability(
        "assess.code", http_method="POST", http_path="/assess/code", http_tags=["assess", "code"], memory="off",
        description=("Assess code through its Explode contract: complexity (cyclomatic, per function), security smells (patterns, "
                     "per card), clones (normalised body hash), tests (public symbols named in tests/), provenance (git log per file), "
                     "and a health composite. Input: path | paths | text + lang (as code.explode), scorers (list), depth (int=1). "
                     "Output: {ok, assessments, receipts, counts, source}."),
    )
    async def cap_assess_code(text: str = "", lang: str = "", path: str = "", paths: Optional[List[str]] = None, scorers: Optional[List[str]] = None,
                              depth: int = 1, trace_id=None) -> Dict:
        X = _explode()
        if isinstance(paths, str):
            paths = [p.strip() for p in paths.split(",") if p.strip()]
        doc = await X.explode_code(text=text, lang=lang, path=path, paths=paths, depth=int(depth or 0))
        if doc.get("error"):
            return doc
        texts = doc["source"]["text"] if isinstance(doc["source"]["text"], dict) else {doc["source"]["paths"][0]: doc["source"]["text"]}
        res = await assess_code_doc(doc, texts, scorers, X._repo_root() if (path or paths) else "")
        return {"ok": True, "assessments": res["assessments"], "receipts": res["receipts"], "counts": doc["counts"], "source": {k: v for k, v in doc["source"].items() if k != "text"}}

    @capability(
        "assess.rank", http_method="POST", http_path="/assess/rank", http_tags=["assess", "fabric"], memory="off",
        description=("Records ordered by a weighted composite of their PERSISTED assessments (assess.prose … persist=true). Input: "
                     "dataset_id (str — optional), keys (list — default every key a record has), weights ({key: w} — default 1), "
                     "invert (list — keys where lower is better; default ['ai_likelihood']), limit (int=50). Output: {ok, count, "
                     "records:[{id, dataset_id, head, composite, scores, missing}], keys, weights, invert}."),
    )
    async def cap_assess_rank(dataset_id: str = "", keys: Optional[List[str]] = None, weights: Optional[Dict[str, float]] = None,
                              invert: Optional[List[str]] = None, limit: int = 50, trace_id=None) -> Dict:
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.split(",") if k.strip()]
        return await asyncio.get_running_loop().run_in_executor(None, rank_records, dataset_id, keys, weights, int(limit or 50), invert)

    log.info("assess: %d scorers registered (%s)", len(_ORDER), ", ".join(_ORDER))
