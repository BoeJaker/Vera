"""Intelligence - decisions and outcomes in the four-minds record shapes (pure).

intel is four-minds' first ledger emitter (ROADMAP §FM-13). Until the shared
ledger (FM-0.2) exists, intel keeps its rows in its own store in EXACTLY the
frozen Decision v0 / Outcome v0 shapes, so `decide.ledger.import` can backfill
them with no translation. Rules agreed with four-minds:
- the row holds a pointer (`state_ref`) and a hash of the state, never the state;
- an uncalibrated score is never `p`: it goes in `detail.score`, `p` stays null;
- `shadow` is only for a second implementation's own answer.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

DECISION_SCHEMA = "vera.decision/v0"
OUTCOME_SCHEMA = "vera.outcome/v0"

#: the questions intel asks, and how each is answered today
QUESTIONS: Dict[str, Dict[str, Any]] = {
    "intel.signal_relevant": {"qtype": "binary", "labels": ["relevant", "not_relevant"],
                              "implementation": "nlp.rerank+blend+threshold"},
    "intel.page_genre": {"qtype": "choice", "labels": ["news", "research", "marketing", "listing", "repo", "index",
                                                       "unsure"],
                         "implementation": "nlp.zeroshot"},
    "intel.worldview_drift": {"qtype": "binary", "labels": ["shifted", "steady"],
                              "implementation": "worldview.detect_drift (health-gated), new over-represented concepts"},
    "intel.entity_resolve": {"qtype": "choice", "labels": ["resolved", "abstain"],
                             "implementation": "wikidata.search+type+site+nlp.rerank"},
    "intel.enrich_worth": {"qtype": "binary", "labels": ["model", "nlp_only"],
                           "implementation": "rule:score>=llm_gate,genre"},
    "intel.material_change": {"qtype": "binary", "labels": ["alert", "no_alert"],
                              "implementation": "rule:material_change"},
}

#: product events: (does it say the item WAS worth it?, strength)
EVENTS = {
    "user.feedback.useful": (True, "strong"),
    "user.feedback.not_useful": (False, "strong"),
    "user.feedback.mute_site": (False, "strong"),
    "link.opened": (True, "medium"),
    "report.button": (True, "medium"),
    "alert.acted": (True, "strong"),
    "alert.ignored": (False, "weak"),
}

#: the answer of each question that means "this item is worth attention"
POSITIVE = {"relevant", "model", "alert", "news", "research", "repo"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def labels_hash(labels: Iterable[str]) -> str:
    return hashlib.sha256(json.dumps(sorted(str(x) for x in labels)).encode()).hexdigest()[:12]


def state_hash(state: Any) -> str:
    """A hash of the rendered state; the state itself is never stored in the row."""
    raw = state if isinstance(state, str) else json.dumps(state, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def make_decision(question_id: str, answer: Any, *, state: Any, state_ref: Dict[str, str],
                  decider: str = "rule", mode: str = "rule", implementation: str = "",
                  p: Optional[float] = None, margin: Optional[float] = None,
                  package: Optional[str] = None, node: Optional[str] = None, ms: int = 0,
                  detail: Optional[Dict[str, Any]] = None, shadow: Optional[Dict[str, Any]] = None,
                  ts: str = "") -> Dict[str, Any]:
    q = QUESTIONS.get(question_id, {})
    labels = list(q.get("labels") or [])
    return {
        "schema": DECISION_SCHEMA,
        "decision_id": "dec_" + uuid.uuid4().hex,
        "ts": ts or _now(),
        "origin": "intel",
        "surface": "intel",
        "question_id": question_id,
        "qtype": q.get("qtype", "binary"),
        "labels": labels,
        "labels_hash": labels_hash(labels),
        "state_hash": state_hash(state),
        "state_ref": {"kind": str(state_ref.get("kind", "")), "id": str(state_ref.get("id", ""))},
        "answer": answer,
        "p": p,
        "margin": margin,
        "decider": decider,
        "implementation": implementation or q.get("implementation", ""),
        "package": package,
        "node": node,
        "ms": int(ms or 0),
        "mode": mode,
        "shadow": shadow,
        "minds_digest": "default",
        "run_id": None, "session_id": None, "turn_id": None,
        "detail": _bounded(detail or {}),
    }


def make_outcome(decision: Dict[str, Any], source: str, *, detail: Optional[Dict[str, Any]] = None,
                 ts: str = "") -> Optional[Dict[str, Any]]:
    """The outcome a product event gives a decision: `correct` when the event agrees
    with what the decision said about the item, `wrong` when it contradicts it
    (rated useful after being judged not relevant, muted after being judged
    relevant). None for an unknown event."""
    ev = EVENTS.get(source)
    if not ev:
        return None
    worth, strength = ev
    said_worth = str(decision.get("answer")) in POSITIVE
    return {"schema": OUTCOME_SCHEMA, "decision_id": decision["decision_id"], "ts": ts or _now(),
            "label": "correct" if worth == said_worth else "wrong", "source": source,
            "strength": strength, "detail": _bounded(detail or {})}


def _bounded(d: Dict[str, Any]) -> Dict[str, Any]:
    """Numbers, booleans and short ids only - never text payload."""
    out: Dict[str, Any] = {}
    for k, v in list(d.items())[:12]:
        if isinstance(v, bool) or isinstance(v, (int, float)) or v is None:
            out[str(k)[:40]] = v
        elif isinstance(v, str) and len(v) <= 64:
            out[str(k)[:40]] = v
    return out


def signal_state(signal: Dict[str, Any], topic_id: str) -> Dict[str, Any]:
    """What a signal decision was taken on - hashed, never stored."""
    return {"topic": topic_id, "url": signal.get("url"), "title": signal.get("title"),
            "score": signal.get("score"), "genre": signal.get("genre"), "novelty": signal.get("novelty")}


#: Signal questions whose inputs a backfill can rebuild from the stored signal.
SIGNAL_QUESTIONS = ("intel.signal_relevant", "intel.page_genre", "intel.enrich_worth")
SNIPPET_CHARS = 1200


def signal_inputs(signal: Dict[str, Any], topic: Dict[str, Any]) -> Dict[str, Any]:
    """What a signal decision saw, for the training store (four-minds FM-13.6): signal_state plus the topic's name
    and the text a person needs to rate it. The store redacts and bounds it; the ledger row still carries only the
    hash."""
    out = signal_state(signal, str(topic.get("id") or ""))
    out.update(topic_name=topic.get("name") or topic.get("title") or "",
               snippet=str(signal.get("summary") or signal.get("snippet") or "")[:SNIPPET_CHARS],
               source=signal.get("site") or signal.get("source") or "")
    return out


def strongest(outcomes: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Calibration uses the strongest outcome of a decision, the latest among equals."""
    order = {"strong": 3, "medium": 2, "weak": 1}
    if not outcomes:
        return None
    return max(outcomes, key=lambda o: (order.get(o.get("strength"), 0), str(o.get("ts") or "")))


def agreement(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Per question: decisions, decisions with an outcome, and how often the
    strongest outcome says the decision was right."""
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        q = out.setdefault(r["question_id"], {"decisions": 0, "with_outcome": 0, "correct": 0})
        q["decisions"] += 1
        o = strongest(r.get("outcomes") or [])
        if o:
            q["with_outcome"] += 1
            q["correct"] += 1 if o["label"] == "correct" else 0
    for q in out.values():
        q["precision"] = round(q["correct"] / q["with_outcome"], 3) if q["with_outcome"] else None
    return out


# NOT an outcome: "another site joined the story" (2026-10-09, removed the same day). The cross-site joins
# were mostly syndicated evergreen/SEO pages (IBM "What is AIOps", marketplace listings, PDFs) that the
# relevance gate had rightly scored 0.8-2.0 against a threshold of 5, so it marked correct `not_relevant`
# decisions wrong. Cross-site duplication is not evidence of relevance.
