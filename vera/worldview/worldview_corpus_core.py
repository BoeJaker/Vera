"""The worldview's curated corpus (four-minds FM-8.3): stratified across collector families, no family above 15%,
Vera's own telemetry left out.

Before 2026-10-10 the training took the first `limit` vectors of the fabric collection in storage order. The
fabric's 1.1M records were then 19% `caps.sandbox` (sandbox command output) and 13% `ide.claude_sessions` (agent
session transcripts) - Vera watching itself - with ~30% market bars across dozens of `mkt.*` series; a model
trained on that learns its own logs. The plan's exit: no dataset above 15% of the assigned records.

- family(): the collector a dataset comes from (`mkt.yahoo.spy.1d` -> `mkt`, `cve_nvd` -> `cve`,
  `prebaked_arxiv_ai` -> `prebaked_arxiv`, `docs_bbc_com` -> `docs`);
- SELF_TELEMETRY: the plan's list (bus.*, ts.*, chat.messages, dream.reports) plus the same category found in the
  store (caps.*, ide.*, streams.*, mesh.*) - configurable as the worldview's `corpus_exclude`;
- quotas(): water-filling - every family gets an equal share of the target, a small family all it has, none more
  than the cap; what a small family leaves goes to the others;
- mix(): the realised mix of the records actually assigned (shares per dataset and per family).

Pure: no I/O.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

FAMILY_CAP = 0.15
#: the plan's self-telemetry (bus.*, ts.*, chat.messages, dream.reports) and the same category found in the store
SELF_TELEMETRY = ("bus.", "ts.", "chat.messages", "dream.reports", "caps.", "ide.", "streams.", "mesh.")
#: underscore-named datasets whose first two words name the collector (prebaked_arxiv_ai -> prebaked_arxiv)
TWO_WORD_FAMILIES = ("prebaked",)


def family(dataset_id: Any) -> str:
    d = str(dataset_id or "").strip()
    if not d:
        return "(none)"
    if "." in d:
        return d.split(".", 1)[0]
    parts = d.split("_")
    if parts[0] in TWO_WORD_FAMILIES and len(parts) > 1:
        return "_".join(parts[:2])
    return parts[0]


def excluded(dataset_id: Any, patterns: Sequence[str] = SELF_TELEMETRY) -> bool:
    """A pattern ending in '.' excludes the prefix; any other excludes the exact dataset."""
    d = str(dataset_id or "")
    return any(d.startswith(p) if p.endswith(".") else d == p for p in patterns)


def quotas(family_sizes: Dict[str, int], target: int, cap: float = FAMILY_CAP) -> Dict[str, int]:
    """Records to take per family: water-filling towards `target`, no family above cap x target (and never more
    than it has). The total is below `target` when the families cannot fill it within the cap."""
    sizes = {f: int(n) for f, n in family_sizes.items() if int(n) > 0}
    target = max(0, int(target))
    if not sizes or not target:
        return {}
    ceiling = max(1, int(cap * target))
    out: Dict[str, int] = {}
    left = target
    for i, (f, n) in enumerate(sorted(sizes.items(), key=lambda kv: (kv[1], kv[0]))):
        share = left // (len(sizes) - i)
        q = min(n, share, ceiling)
        out[f] = q
        left -= q
    return out


def mix(dataset_ids: Iterable[Any], top: int = 8) -> Dict[str, Any]:
    """The realised mix of a set of records (their dataset ids): shares per dataset and per family."""
    ds = Counter(str(d or "") for d in dataset_ids)
    n = sum(ds.values())
    if not n:
        return {"records": 0}
    fam: Counter = Counter()
    for d, c in ds.items():
        fam[family(d)] += c
    return {"records": n, "datasets": len(ds), "families": len(fam),
            "max_dataset_share": round(max(ds.values()) / n, 4),
            "max_family_share": round(max(fam.values()) / n, 4),
            "top_datasets": [[d, c] for d, c in ds.most_common(top)],
            "top_families": [[f, c] for f, c in fam.most_common(top)]}


def plan(dataset_counts: Dict[str, int], target: int, exclude: Sequence[str] = SELF_TELEMETRY,
         cap: float = FAMILY_CAP) -> Dict[str, Any]:
    """{quotas: {family: n}, datasets: {family: [dataset ids]}, excluded: [dataset ids], eligible: n} from the
    store's per-dataset counts."""
    by_family: Dict[str, List[str]] = {}
    sizes: Counter = Counter()
    dropped: List[str] = []
    for d, n in dataset_counts.items():
        if excluded(d, exclude):
            dropped.append(d)
            continue
        f = family(d)
        by_family.setdefault(f, []).append(d)
        sizes[f] += int(n)
    return {"quotas": quotas(dict(sizes), target, cap), "datasets": by_family,
            "excluded": sorted(dropped), "eligible": int(sum(sizes.values()))}



def interleave(groups: Dict[str, Sequence[str]]) -> List[str]:
    """Round-robin over the families' id lists (families in name order): fetching the vectors in this order and
    stopping early still leaves every family its share, where a family-by-family list cut short drops the last
    families whole (the first v3 training, 11 Oct: ~40k ids in one Chroma get timed out)."""
    lists = [list(groups[k]) for k in sorted(groups) if groups[k]]
    out: List[str] = []
    for i in range(max((len(x) for x in lists), default=0)):
        out.extend(x[i] for x in lists if i < len(x))
    return out


def batches(ids: Sequence[str], size: int) -> List[List[str]]:
    size = max(1, int(size or 1))
    return [list(ids[i:i + size]) for i in range(0, len(ids), size)]


class FamilyTake:
    """Which fetched records join the corpus: at most each family's quota and `limit` in all - the oversampled
    pick has spare ids (not every record has a vector yet), this keeps the plan's shares when they all do."""

    def __init__(self, family_quotas: Dict[str, int], limit: int):
        self.quotas = {k: int(v) for k, v in (family_quotas or {}).items()}
        self.limit = int(limit)
        self.got: Counter = Counter()
        self.n = 0

    @property
    def full(self) -> bool:
        return self.n >= self.limit

    def take(self, dataset_id: Any) -> bool:
        f = family(dataset_id)
        if self.full or self.got[f] >= self.quotas.get(f, 0):
            return False
        self.got[f] += 1
        self.n += 1
        return True


def fetch_by_ids(get: Callable[[List[str]], Tuple[List[str], List[Any], Dict[str, Dict[str, Any]]]],
                 ids: Sequence[str], size: int = 500, take: "Optional[FamilyTake]" = None, cap: int = 50000,
                 max_fails: int = 3, on_fail: Optional[Callable[[int, int, Exception], None]] = None
                 ) -> Tuple[List[str], List[Any], Dict[str, Dict[str, Any]], Dict[str, int]]:
    """Vectors for an id set in bounded batches: get(batch) -> (rids, rows, meta by rid). A failed batch is skipped;
    `max_fails` in a row end the fetch with what came. `take` picks the records that join (and ends the fetch when
    full); `cap` bounds the result. Returns (rids, rows, meta, {batches, failed, fetched})."""
    rids: List[str] = []
    rows: List[Any] = []
    meta: Dict[str, Dict[str, Any]] = {}
    info = {"batches": 0, "failed": 0, "fetched": 0}
    fails = 0
    for batch in batches(list(ids), size):
        if (take is not None and take.full) or len(rids) >= cap:
            break
        info["batches"] += 1
        try:
            r, w, m = get(batch)
        except Exception as e:  # a slow or dropped batch costs that batch, not the training
            fails += 1
            info["failed"] += 1
            if on_fail is not None:
                on_fail(len(batch), fails, e)
            if fails >= max_fails:
                break
            continue
        fails = 0
        info["fetched"] += len(r)
        for rid, row in zip(r, w):
            if len(rids) >= cap:
                break
            if take is not None and not take.take((m.get(rid) or {}).get("dataset_id", "")):
                continue
            rids.append(rid)
            rows.append(row)
            meta[rid] = m.get(rid) or {}
    return rids, rows, meta, info


SNAPSHOT_DATASET = "vera.worldview.corpus"
SNAPSHOT_SCHEMA = {"record": "fabric_record_ref/v1", "fields": ["id", "dataset_id"]}


def snapshot_records(rids, records) -> List[Dict[str, str]]:
    """The records a training used, as references (id + dataset), ordered by id: the same corpus always gives the
    same DatasetSnapshot id, whatever order the graph held them in."""
    return [{"id": str(r), "dataset_id": str(((records or {}).get(r) or {}).get("dataset_id") or "")}
            for r in sorted(set(str(x) for x in (rids or [])))]
