"""A known-item retrieval eval set (four-minds FM-7.0c): real fabric records, each searched by the distinctive words
it opens with, the record itself the one right answer.

The implicit eval set (seek then read, chat recall then used) held 4 labelled seeks in a week (FM-0.4d) - too few to
read a ranking change. A known-item set needs no labeller and no model: it asks "does memory.seek put THIS record near
the top when asked for what it says?". It measures ranking (MRR, nDCG@10, hit@k) and latency together, which is what
FM-7.2 (recency x importance) and FM-7.3 (rerank) must improve without slowing seek. It does not measure recency or
temporal intent - those need dated questions.

Records are drawn from a deterministic every-Nth-row sample, stratified by collector family (worldview_corpus_core:
no family above the cap, self-telemetry left out), so the set is the fabric's breadth, not its biggest dataset.

Pure: no I/O.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

try:                                     # pragma: no cover - import-shape only
    from Vera.vera.worldview import worldview_corpus_core as WC
except Exception:                        # worktree / app-free import
    from vera.worldview import worldview_corpus_core as WC   # type: ignore
try:                                     # pragma: no cover - import-shape only
    from Vera.vera.models import training_contracts as TCX
except Exception:                        # worktree / app-free import
    from vera.models import training_contracts as TCX   # type: ignore

VERSION = "v1"
MIN_TEXT = 80
QUERY_WORDS = 6
MIN_QUERY_WORDS = 3
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9'\-]{2,}")
STOP = set("""the and for with that this from have has had are was were will would could should into onto about
over under than then them they their there these those what when where which while who whom whose why how your yours
you our ours its it's not but can may might must shall been being also just more most some such only very each other
any all both few many much own same per via out off upon within without between among after before during again
further here once does did doing done make made get got new one two three four five six seven eight nine ten
http https www com org net html""".split())


def query_from(text: str, n: int = QUERY_WORDS) -> str:
    """The first n distinctive words of a record (no stopwords, no repeats, letters first, 3+ characters), or '' when
    it has fewer than MIN_QUERY_WORDS - such a record would test nothing."""
    out: List[str] = []
    seen = set()
    for w in _WORD.findall(str(text or "")[:600]):
        lw = w.lower().strip("'-")
        if len(lw) < 3 or lw in STOP or lw in seen:
            continue
        seen.add(lw)
        out.append(lw)
        if len(out) >= n:
            break
    return " ".join(out) if len(out) >= MIN_QUERY_WORDS else ""


def _order(rid: str, seed: str) -> str:
    return hashlib.sha1(f"{seed}|{rid}".encode("utf-8")).hexdigest()


def pick(candidates: Iterable[Dict[str, Any]], n: int, seed: str = VERSION,
         cap: float = WC.FAMILY_CAP, exclude: Sequence[str] = WC.SELF_TELEMETRY) -> List[Dict[str, Any]]:
    """Up to n items {id, dataset_id, family, query} from candidate records {id, dataset_id, text}: self-telemetry and
    records without a usable query left out, families round-robin in a seed-fixed order, none above cap * n."""
    by_family: Dict[str, List[Dict[str, Any]]] = {}
    for c in candidates:
        rid, ds = str(c.get("id") or ""), str(c.get("dataset_id") or "")
        if not rid or WC.excluded(ds, exclude) or len(str(c.get("text") or "")) < MIN_TEXT:
            continue
        q = query_from(c.get("text") or "")
        if not q:
            continue
        fam = WC.family(ds)
        by_family.setdefault(fam, []).append({"id": rid, "dataset_id": ds, "family": fam, "query": q})
    for fam in by_family:
        by_family[fam].sort(key=lambda it: _order(it["id"], seed))
    # no family above cap * n - unless there are too few families to fill n under it (fewer than 1/cap), when each
    # family may take an equal share instead
    limit = max(1, int(math.floor(cap * n)), int(math.ceil(n / max(1, len(by_family))))) if n > 0 else 0
    order = WC.interleave({f: [it["id"] for it in items] for f, items in by_family.items()})
    item_of = {it["id"]: it for items in by_family.values() for it in items}
    got: Dict[str, int] = {}
    out: List[Dict[str, Any]] = []
    for rid in order:
        it = item_of[rid]
        if got.get(it["family"], 0) >= limit:
            continue
        got[it["family"]] = got.get(it["family"], 0) + 1
        out.append(it)
        if len(out) >= n:
            break
    return out


def rank_of(expected: str, ranked_ids: Sequence[str]) -> Optional[int]:
    """1-based rank of the expected record, None when it is not in the list."""
    for i, rid in enumerate(ranked_ids or [], 1):
        if str(rid) == str(expected):
            return i
    return None


def _pct(values: Sequence[float], q: float) -> Optional[float]:
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    return v[max(0, min(len(v) - 1, int(math.ceil(q * len(v))) - 1))]


def summarize(ranks: Sequence[Optional[int]], latencies_ms: Sequence[Optional[float]] = (), k: int = 10) -> Dict[str, Any]:
    """MRR, nDCG@k (one relevant item: 1/log2(rank+1)), hit@1/5/k and latency p50/p95 over a run."""
    n = len(ranks)
    if not n:
        return {"n": 0}
    rr = [1.0 / r if r else 0.0 for r in ranks]
    nd = [1.0 / math.log2(r + 1) if r and r <= k else 0.0 for r in ranks]
    hit = lambda m: round(sum(1 for r in ranks if r and r <= m) / n, 3)
    return {"n": n, "mrr": round(sum(rr) / n, 3), f"ndcg@{k}": round(sum(nd) / n, 3), "hit@1": hit(1),
            "hit@5": hit(5), f"hit@{k}": hit(k), "missing": sum(1 for r in ranks if not r),
            "ms_p50": _pct(latencies_ms, 0.5), "ms_p95": _pct(latencies_ms, 0.95)}


def compare(base: Dict[str, Any], arm: Dict[str, Any], k: int = 10) -> Dict[str, Any]:
    """The arm minus the base on every headline number (positive = the arm is better, except latency)."""
    keys = ("mrr", f"ndcg@{k}", "hit@1", "hit@5", f"hit@{k}", "ms_p50", "ms_p95")
    return {key: (round(arm[key] - base[key], 3) if isinstance(arm.get(key), (int, float))
                  and isinstance(base.get(key), (int, float)) else None) for key in keys}


# ── the run as a W5-03 EvaluationReport (backbone review 11 Oct 2026: one evaluation contract, not a third format) ──
# The subject is memory.seek with the run's switch modes; the dataset revision is the set's content (its record ids and
# queries), so two runs are comparable exactly when they ran the same cases. W5-04 holds that live search is not
# admissible evidence for a frozen snapshot comparison: these reports compare seek arms against each other on the live
# fabric, nothing more.
REPORT_METRICS = ("hit_at_10", "mrr", "ndcg_at_10")
PROVIDER_ID = "vera.retrieval.known_items"


def set_revision(items: Iterable[Dict[str, Any]]) -> str:
    """known_items:<sha> over the set's (record id, query) pairs - the cases, not the file."""
    pairs = sorted((str(it.get("id") or ""), str(it.get("query") or "")) for it in items)
    return "known_items:" + hashlib.sha256(repr(pairs).encode("utf-8")).hexdigest()[:32]


def subject_id(minds: Optional[Dict[str, Any]] = None) -> str:
    """memory.seek, then each switch mode the run set (identifier-safe: name:mode pairs)."""
    safe = lambda x: re.sub(r"[^A-Za-z0-9._/+-]", "-", str(x))  # noqa: E731
    parts = ["memory.seek"] + [f"{safe(k)}:{safe(v)}" for k, v in sorted((minds or {}).items())]
    return ":".join(parts)[:256]


def evaluation_report(set_rev: str, minds: Optional[Dict[str, Any]], summary: Dict[str, Any],
                      baseline: Optional[Dict[str, Any]] = None, k: int = 10) -> Dict[str, Any]:
    """The run's summary as a W5-03 EvaluationReport dict. Thresholds are the baseline's values when one is given (an arm
    passes a metric by matching or beating it), else 0. A case is failed when its record was not retrieved at all."""
    req = TCX.EvaluationRequest("capability", subject_id(minds), set_rev, REPORT_METRICS)
    n = int(summary.get("n") or 0)
    if not n:
        return TCX.EvaluationReport(req, PROVIDER_ID, "failed", error_code="empty_run").to_dict()
    key = {"hit_at_10": f"hit@{k}", "mrr": "mrr", "ndcg_at_10": f"ndcg@{k}"}
    metrics = tuple(TCX.MetricResult(m, float(summary.get(key[m]) or 0.0),
                                     float((baseline or {}).get(key[m]) or 0.0)) for m in REPORT_METRICS)
    return TCX.EvaluationReport(req, PROVIDER_ID, "completed", metrics=metrics, evaluated_cases=n,
                                failed_cases=int(summary.get("missing") or 0)).to_dict()
