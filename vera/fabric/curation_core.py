"""
curation_core.py — pure, dependency-free helpers for the curated-dataset layer
==============================================================================

Split out from curation_capabilities.py so the identity / merge / select-SQL /
validation logic can be unit-tested WITHOUT booting the whole app (the cap
module imports the orchestrator, which is heavy). Nothing here imports anything
beyond the stdlib.
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Dict, List, Optional, Tuple

# Allowed operators for a memory.select `where` clause → SQL comparison.
OPS = {
    "eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
    "contains": "LIKE", "startswith": "LIKE", "endswith": "LIKE",
    "in": "IN", "exists": "IS NOT NULL",
}

FIELD_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

TYPE_CHECK = {
    "string":  lambda v: isinstance(v, str),
    "number":  lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array":   lambda v: isinstance(v, list),
    "object":  lambda v: isinstance(v, dict),
}

_EMPTY = (None, "", [], {})


def key_id(dataset_id: str, row: Dict[str, Any], key_fields: List[str]) -> str:
    """Deterministic record id from a business key. Same (dataset, key values) →
    same id, so a re-ingest REPLACES the row rather than duplicating it."""
    vals = "\x00".join(str(row.get(k, "")) for k in key_fields)
    digest = hashlib.sha256(f"{dataset_id}\x00{vals}".encode()).hexdigest()[:24]
    return f"k_{digest}"


def merge_row(prev: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    """Gap-fill merge: start from the stored row, then overlay the incoming
    NON-EMPTY fields. A partial fetch tops up a fuller stored row rather than
    clobbering it; present incoming values win."""
    merged = dict(prev or {})
    for k, v in (incoming or {}).items():
        if k == "_id":
            continue
        if v not in _EMPTY:
            merged[k] = v
    return merged


def json_path(field: str) -> str:
    """A json_extract path for a field that has already passed FIELD_RE."""
    return f"json_extract(data, '$.{field}')"


def build_select_sql(dataset_id: str, where: List[Dict[str, Any]],
                     sort: List[Dict[str, Any]], limit: int,
                     offset: int) -> Tuple[str, List[Any]]:
    """Build a parametrised SELECT over fabric_records for a field-level query.
    Only the operator and a sanitised field path are interpolated; every value
    is bound as a parameter."""
    clauses = ["dataset_id = ?"]
    params: List[Any] = [dataset_id]
    for cond in (where or []):
        if not isinstance(cond, dict):
            continue
        field = str(cond.get("field", ""))
        op = str(cond.get("op", "eq")).lower()
        if not FIELD_RE.match(field) or op not in OPS:
            continue
        val = cond.get("value")
        path = json_path(field)
        if op == "exists":
            clauses.append(f"{path} IS NOT NULL")
        elif op == "in":
            vals = val if isinstance(val, list) else [val]
            if not vals:
                continue
            clauses.append(f"{path} IN (%s)" % ",".join("?" * len(vals)))
            params.extend([str(v) for v in vals])
        elif op in ("contains", "startswith", "endswith"):
            s = str(val)
            like = {"contains": f"%{s}%", "startswith": f"{s}%",
                    "endswith": f"%{s}"}[op]
            clauses.append(f"{path} LIKE ?")
            params.append(like)
        else:
            clauses.append(f"{path} {OPS[op]} ?")
            params.append(val)
    sql = ("SELECT id, data, text, created_at FROM fabric_records "
           f"WHERE {' AND '.join(clauses)}")
    order_bits = []
    for s in (sort or []):
        if not isinstance(s, dict):
            continue
        field = str(s.get("field", ""))
        if not FIELD_RE.match(field):
            continue
        direction = ("DESC" if str(s.get("dir", "asc")).lower() in ("desc", "d", "-1")
                     else "ASC")
        order_bits.append(f"{json_path(field)} {direction}")
    if order_bits:
        sql += " ORDER BY " + ", ".join(order_bits)
    sql += " LIMIT ? OFFSET ?"
    params.extend([int(limit), int(offset)])
    return sql, params


def normalise_schema(schema: Dict[str, Any],
                     key_fields: List[str]) -> Dict[str, Any]:
    """Normalise a declared schema: each field → {type, required, description}.
    Key fields are forced into the schema as required. Raises ValueError on a
    bad field name."""
    norm: Dict[str, Any] = {}
    for fname, spec in (schema or {}).items():
        if not FIELD_RE.match(str(fname)):
            raise ValueError(f"invalid field name: {fname!r}")
        if isinstance(spec, str):
            spec = {"type": spec}
        elif not isinstance(spec, dict):
            spec = {"type": "string"}
        norm[fname] = {
            "type": str(spec.get("type", "string")),
            "required": bool(spec.get("required", False)),
            "description": str(spec.get("description", "")),
        }
    for k in key_fields:
        if k not in norm:
            norm[k] = {"type": "string", "required": True, "description": "key"}
    return norm


def validate_rows(rows: List[Dict[str, Any]], schema: Dict[str, Any],
                  key_fields: List[str], declared_trust: float = 0.3) -> Dict[str, Any]:
    """Score a sample of rows against a declared schema. Returns coverage,
    required-violations, type errors, duplicate-key count, a 0..1 quality score
    and an effective trust that blends declared trust with measured quality."""
    sampled = len(rows)
    fields = list(schema.keys()) or sorted({k for r in rows for k in r.keys()})
    coverage: Dict[str, float] = {}
    for f in fields:
        present = sum(1 for r in rows if r.get(f) not in _EMPTY)
        coverage[f] = round(present / sampled, 4) if sampled else 0.0

    missing_required: Dict[str, int] = {}
    type_errors: Dict[str, int] = {}
    for f, spec in (schema or {}).items():
        req = spec.get("required")
        checker = TYPE_CHECK.get(spec.get("type", "string"))
        for r in rows:
            v = r.get(f)
            if v in _EMPTY:
                if req:
                    missing_required[f] = missing_required.get(f, 0) + 1
                continue
            if checker and not checker(v):
                type_errors[f] = type_errors.get(f, 0) + 1

    duplicate_keys = 0
    if key_fields:
        seen: Dict[str, int] = {}
        for r in rows:
            kv = "\x00".join(str(r.get(k, "")) for k in key_fields)
            seen[kv] = seen.get(kv, 0) + 1
        duplicate_keys = sum(c - 1 for c in seen.values() if c > 1)

    problems = sum(missing_required.values()) + sum(type_errors.values()) + duplicate_keys
    denom = max(1, sampled)
    quality_score = round(max(0.0, 1.0 - problems / denom), 4)
    ok = (not missing_required) and (not type_errors) and duplicate_keys == 0
    eff_trust = round(declared_trust * (0.5 + 0.5 * quality_score), 4)
    return {
        "sampled": sampled, "coverage": coverage,
        "missing_required": missing_required, "type_errors": type_errors,
        "duplicate_keys": duplicate_keys, "quality_score": quality_score,
        "trust": eff_trust, "ok": ok,
    }


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 2 — identity, gaps (+ noise/backoff suppression), fusion
# ─────────────────────────────────────────────────────────────────────────────

def match_score(candidate: Dict[str, Any], *, subject: str,
                expected_fields: List[str], want_tags: List[str]) -> float:
    """Deterministic 0..1 score for how well an existing dataset matches what the
    caller is looking for. Blends: id/subject token overlap, tag overlap, and
    declared-schema field overlap (schema fingerprint)."""
    did = str(candidate.get("dataset_id", "")).lower()
    subj = (subject or "").lower()
    subj_tokens = set(re.findall(r"[a-z0-9]+", subj))
    id_tokens = set(re.findall(r"[a-z0-9]+", did))
    id_overlap = (_jacc(subj_tokens, id_tokens) if subj_tokens else 0.0)
    # substring bonus: "pokedex" fully inside "pokedex.gen1"
    if subj and subj in did:
        id_overlap = max(id_overlap, 0.9)

    cand_tags = {str(t).lower() for t in (candidate.get("tags") or [])}
    tag_overlap = (_jacc(set(t.lower() for t in want_tags), cand_tags)
                   if want_tags else 0.0)

    cand_fields = set(candidate.get("fields") or [])
    field_overlap = (len(set(expected_fields) & cand_fields) / len(set(expected_fields))
                     if expected_fields else 0.0)

    # Weighted: id/subject is the strongest signal, then fields, then tags.
    parts, weights = [], []
    if subj_tokens:
        parts.append(id_overlap); weights.append(0.5)
    if expected_fields:
        parts.append(field_overlap); weights.append(0.35)
    if want_tags:
        parts.append(tag_overlap); weights.append(0.15)
    if not parts:
        return 0.0
    return round(sum(p * w for p, w in zip(parts, weights)) / sum(weights), 4)


def _jacc(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b) if (a | b) else 0.0


def compute_field_gaps(rows: List[Dict[str, Any]], expected_fields: List[str],
                       coverage_threshold: float = 0.9) -> List[Dict[str, Any]]:
    """Fields whose non-null coverage across `rows` falls below the threshold.
    Each gap: {type:'field', ref:<field>, coverage, missing}."""
    n = len(rows)
    gaps: List[Dict[str, Any]] = []
    for f in expected_fields:
        present = sum(1 for r in rows if r.get(f) not in _EMPTY)
        cov = (present / n) if n else 0.0
        if cov < coverage_threshold:
            gaps.append({"type": "field", "ref": f,
                         "coverage": round(cov, 4), "missing": n - present})
    return gaps


def compute_key_gaps(present_keys: set, *, expected_keys: Optional[List[Any]] = None,
                     key_range: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Missing business keys. Either an explicit `expected_keys` list, or a
    numeric `key_range` {min, max} that is expanded to the integer range. Each
    gap: {type:'key', ref:<missing key as str>}."""
    present = {str(k) for k in present_keys}
    want: List[str] = []
    if expected_keys:
        want = [str(k) for k in expected_keys]
    elif key_range and "min" in key_range and "max" in key_range:
        try:
            lo, hi = int(key_range["min"]), int(key_range["max"])
            want = [str(i) for i in range(lo, hi + 1)]
        except Exception:
            want = []
    return [{"type": "key", "ref": k} for k in want if k not in present]


def gap_id(dataset_id: str, gap: Dict[str, Any]) -> str:
    """Stable identity for a gap so the ledger can track attempts on it."""
    raw = f"{dataset_id}\x00{gap.get('type','')}\x00{gap.get('ref','')}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


# Backoff so a gap that keeps failing to fill is retried ever-less-often instead
# of re-triggering a fetch every cycle.
BACKOFF_BASE_SECS = 300         # 5 min after the 1st failed attempt
BACKOFF_CAP_SECS = 7 * 86400    # never wait more than a week
MAX_FILL_ATTEMPTS = 4           # after this many failures → auto 'unfillable'


def backoff_cooldown(attempts: int, *, base: int = BACKOFF_BASE_SECS,
                     cap: int = BACKOFF_CAP_SECS) -> int:
    """Seconds to wait before the next attempt, doubling per attempt, capped."""
    if attempts <= 0:
        return 0
    return int(min(cap, base * (2 ** (attempts - 1))))


def gap_actionable(ledger: Optional[Dict[str, Any]], *, now: Optional[float] = None,
                   max_attempts: int = MAX_FILL_ATTEMPTS) -> Tuple[bool, str]:
    """Decide whether a gap should trigger a fetch RIGHT NOW, given its ledger
    row (or None for a never-seen gap). Returns (actionable, reason).

    Suppressed when: explicitly marked noise/unfillable/filled, attempts exhausted,
    or still inside the backoff cooldown. This is the guard that stops noisy or
    un-fillable gaps from continually re-triggering fetches."""
    now = time.time() if now is None else now
    if ledger is None:
        return True, "new"
    status = (ledger.get("status") or "open").lower()
    if status in ("noise", "unfillable"):
        return False, status
    if status == "filled":
        return False, "filled"
    if int(ledger.get("attempts", 0)) >= max_attempts:
        return False, "attempts_exhausted"
    cooldown = ledger.get("cooldown_until_ts")
    try:
        if cooldown is not None and float(cooldown) > now:
            return False, "cooldown"
    except (TypeError, ValueError):
        pass
    return True, "open"


def rank_context_datasets(cands: List[Dict[str, Any]], *, top: int) -> List[Dict[str, Any]]:
    """Rank candidate datasets for context injection: TRUST first (curated data
    is more authoritative than free-text memory), then query relevance, then a
    tag-match bonus. Deduped by dataset_id (best kept)."""
    best: Dict[str, Dict[str, Any]] = {}
    for c in cands:
        did = c.get("dataset_id")
        if not did:
            continue
        prev = best.get(did)
        if prev is None or _ctx_rank_key(c) > _ctx_rank_key(prev):
            best[did] = c
    ranked = sorted(best.values(), key=_ctx_rank_key, reverse=True)
    return ranked[:max(0, top)]


def _ctx_rank_key(d: Dict[str, Any]) -> Tuple[float, float, float]:
    return (float(d.get("trust", 0) or 0),
            float(d.get("relevance", 0) or 0),
            float(d.get("tag_match", 0) or 0))


def join_rows(left: List[Dict[str, Any]], right: List[Dict[str, Any]],
              on: List[str], how: str = "inner") -> List[Dict[str, Any]]:
    """Row-level join of two row lists on the `on` field(s). LEFT wins on field
    conflicts. how ∈ inner|left|outer. Returns the fused rows."""
    how = (how or "inner").lower()

    def keyof(r: Dict[str, Any]) -> Tuple:
        return tuple(str(r.get(k, "")) for k in on)

    ridx: Dict[Tuple, List[Dict[str, Any]]] = {}
    for r in right:
        ridx.setdefault(keyof(r), []).append(r)

    out: List[Dict[str, Any]] = []
    matched_keys: set = set()
    for l in left:
        k = keyof(l)
        matches = ridx.get(k)
        if matches:
            matched_keys.add(k)
            for rr in matches:
                out.append({**rr, **l})   # left overrides right on conflict
        elif how in ("left", "outer"):
            out.append(dict(l))
    if how == "outer":
        left_keys = {keyof(l) for l in left}
        for rr in right:
            if keyof(rr) not in left_keys:
                out.append(dict(rr))
    return out
