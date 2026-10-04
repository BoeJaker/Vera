"""The decision ledger's record rules (four-minds FM-0.2, ROADMAP §5.1).

Decision v0 and Outcome v0 were frozen on 2026-10-04 so early emitters (intel first) could
keep rows in their own stores and import them later without translation. This module is
the one place those shapes are checked, minted and mapped to storage:

- `make_decision` / `make_outcome` mint rows for any surface;
- `check_decision` / `check_outcome` return the problems with a row ([] = valid), so the
  ledger refuses a bad row at the door instead of storing something calibration can't use;
- `partition_for` names the monthly Postgres partition a row lands in;
- `strongest` picks the outcome calibration uses when a decision has several.

The row holds a pointer and a hash of the state, never the state: `detail` is bounded and
no string in a row may run past MAX_STR.

Pure: no I/O.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

DECISION_SCHEMA = "vera.decision/v0"
OUTCOME_SCHEMA = "vera.outcome/v0"

ORIGINS = frozenset({"census", "delegate", "user", "schedule", "background", "intel", "chat",
                     "canvas", "discovery"})
QTYPES = frozenset({"binary", "choice", "score", "ranking"})
DECIDERS = frozenset({"rule", "student", "llm", "human"})
MODES = frozenset({"llm_only", "shadow", "sampled", "student_first", "rule"})
STRENGTHS = ("weak", "medium", "strong")          # ascending

MAX_STR = 300            # longest string anywhere in a row: pointers and labels, not text
MAX_DETAIL_BYTES = 2000  # serialised `detail`
_ID_RE = re.compile(r"^dec_[0-9a-f]{32}$")
_QID_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")

DECISION_REQUIRED = ("schema", "decision_id", "ts", "origin", "surface", "question_id", "qtype",
                     "labels", "labels_hash", "state_hash", "state_ref", "answer", "decider",
                     "implementation", "mode")
OUTCOME_REQUIRED = ("schema", "decision_id", "ts", "label", "source", "strength")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_decision_id() -> str:
    return "dec_" + uuid.uuid4().hex


def labels_hash(labels: Iterable[str]) -> str:
    return hashlib.sha256(json.dumps(sorted(str(x) for x in labels)).encode()).hexdigest()[:12]


def state_hash(state: Any) -> str:
    raw = state if isinstance(state, str) else json.dumps(state, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def make_decision(question_id: str, answer: Any, *, origin: str, surface: str, qtype: str,
                  labels: List[str], state: Any, state_ref: Dict[str, str],
                  decider: str = "rule", implementation: str = "", mode: str = "rule",
                  p: Optional[float] = None, margin: Optional[float] = None,
                  package: Optional[str] = None, node: Optional[str] = None, ms: int = 0,
                  shadow: Optional[Dict[str, Any]] = None, minds_digest: str = "default",
                  run_id: Optional[str] = None, session_id: Optional[str] = None,
                  turn_id: Optional[str] = None, detail: Optional[Dict[str, Any]] = None,
                  ts: str = "") -> Dict[str, Any]:
    labels = [str(x) for x in labels]
    return {
        "schema": DECISION_SCHEMA, "decision_id": new_decision_id(), "ts": ts or now_iso(),
        "origin": origin, "surface": surface, "question_id": question_id, "qtype": qtype,
        "labels": labels, "labels_hash": labels_hash(labels),
        "state_hash": state_hash(state), "state_ref": dict(state_ref or {}),
        "answer": answer, "p": p, "margin": margin, "decider": decider,
        "implementation": implementation, "package": package, "node": node, "ms": int(ms or 0),
        "mode": mode, "shadow": shadow, "minds_digest": minds_digest or "default",
        "run_id": run_id, "session_id": session_id, "turn_id": turn_id, "detail": dict(detail or {}),
    }


def make_outcome(decision_id: str, label: Any, source: str, strength: str = "medium",
                 detail: Optional[Dict[str, Any]] = None, ts: str = "") -> Dict[str, Any]:
    return {"schema": OUTCOME_SCHEMA, "decision_id": decision_id, "ts": ts or now_iso(),
            "label": label, "source": source, "strength": strength, "detail": dict(detail or {})}


def _parse_ts(ts: Any) -> Optional[datetime]:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _long_strings(v: Any, path: str = "") -> List[str]:
    if isinstance(v, str):
        return [path or "value"] if len(v) > MAX_STR else []
    if isinstance(v, dict):
        return [p for k, x in v.items() for p in _long_strings(x, f"{path}.{k}" if path else str(k))]
    if isinstance(v, (list, tuple)):
        return [p for i, x in enumerate(v) for p in _long_strings(x, f"{path}[{i}]")]
    return []


def _common(row: Dict[str, Any], required, schema: str) -> List[str]:
    if not isinstance(row, dict):
        return ["not an object"]
    errs = [f"missing {k}" for k in required if k not in row]
    if row.get("schema") != schema:
        errs.append(f"schema is not {schema}")
    if not _ID_RE.match(str(row.get("decision_id", ""))):
        errs.append("decision_id is not dec_<32 hex>")
    if _parse_ts(row.get("ts")) is None:
        errs.append("ts is not ISO-8601")
    detail = row.get("detail", {})
    if detail is not None and not isinstance(detail, dict):
        errs.append("detail is not an object")
    elif len(json.dumps(detail or {}, default=str)) > MAX_DETAIL_BYTES:
        errs.append(f"detail over {MAX_DETAIL_BYTES} bytes")
    errs += [f"{p} longer than {MAX_STR} chars (pointers, not payloads)" for p in _long_strings(row)]
    return errs


def check_decision(row: Dict[str, Any]) -> List[str]:
    errs = _common(row, DECISION_REQUIRED, DECISION_SCHEMA)
    if errs and errs[0] == "not an object":
        return errs
    for key, allowed in (("origin", ORIGINS), ("qtype", QTYPES), ("decider", DECIDERS), ("mode", MODES)):
        if key in row and row[key] not in allowed:
            errs.append(f"{key} {row[key]!r} not one of {sorted(allowed)}")
    if "question_id" in row and not _QID_RE.match(str(row["question_id"])):
        errs.append("question_id is not dotted lowercase")
    if not isinstance(row.get("labels", []), list):
        errs.append("labels is not a list")
    elif "labels_hash" in row and row["labels_hash"] != labels_hash(row["labels"]):
        errs.append("labels_hash does not match labels")
    if not isinstance(row.get("state_ref", {}), dict):
        errs.append("state_ref is not an object")
    p = row.get("p")
    if p is not None and not (isinstance(p, (int, float)) and 0.0 <= p <= 1.0):
        errs.append("p is not null or 0..1 (an uncalibrated score goes in detail.score)")
    return errs


def check_outcome(row: Dict[str, Any]) -> List[str]:
    errs = _common(row, OUTCOME_REQUIRED, OUTCOME_SCHEMA)
    if errs and errs[0] == "not an object":
        return errs
    if "strength" in row and row["strength"] not in STRENGTHS:
        errs.append(f"strength {row['strength']!r} not one of {list(STRENGTHS)}")
    return errs


def partition_for(ts: str) -> Dict[str, str]:
    """The monthly partition of decision_ledger holding a row with this ts."""
    d = _parse_ts(ts) or datetime.now(timezone.utc)
    start = datetime(d.year, d.month, 1, tzinfo=timezone.utc)
    end = datetime(d.year + (d.month == 12), d.month % 12 + 1, 1, tzinfo=timezone.utc)
    return {"name": f"decision_ledger_y{d.year:04d}m{d.month:02d}",
            "from": start.strftime("%Y-%m-%d"), "to": end.strftime("%Y-%m-%d")}


def strongest(outcomes: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The outcome calibration uses: the strongest, and the latest among equals."""
    best, key = None, None
    for o in outcomes or []:
        k = (STRENGTHS.index(o["strength"]) if o.get("strength") in STRENGTHS else -1, str(o.get("ts", "")))
        if key is None or k > key:
            best, key = o, k
    return best
