"""Relevance x recency x importance for memory.seek (four-minds FM-7.2, stage-0 rules).

    score = fused_relevance x time_weight(age, class, temporal intent) x (0.5 + 0.5 * importance)

Before this, seek gave every record the same mild boost (+25% at most, 45-day e-folding) whatever it was: a 3-day-old
news page and a 3-day-old design note aged alike, and "latest" in the question changed nothing. Here each dataset
class has its own half-life (the plan's initial values, to tune on a dated eval set), a floor keeps a strong old match
from vanishing, and the question's own words set the temporal intent (rules first; a student later): "latest",
"today", "current" halve the half-life; "originally", "first", "history" switch decay off.

Not here yet (the plan's other factors, no data to drive them): source authority, and reinforcement from recalled-and-
used items.

Pure: no I/O.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

try:                                     # pragma: no cover - import-shape only
    from Vera.vera.worldview import worldview_corpus_core as WC
except Exception:                        # worktree / app-free import
    from vera.worldview import worldview_corpus_core as WC   # type: ignore

#: half-life in days per class (None = no age decay)
HALF_LIFE: Dict[str, Optional[float]] = {
    "news": 3.0, "chat": 14.0, "research": 60.0, "docs": None, "telemetry": 1.0, "fact": None, "delegate": 30.0,
    "default": 30.0,
}
FLOOR = 0.2
#: dataset family / memory category -> class (first match wins)
_CLASS_RULES = (
    (re.compile(r"^memory\.(fact|preference|profile|note)"), "fact"),
    (re.compile(r"^memory\.(delegate|trajectory)"), "delegate"),
    (re.compile(r"^memory\.|^chat\."), "chat"),
    (re.compile(r"^(web|news|rss|feed|intel|docs_|discover)"), "news"),
    (re.compile(r"^(mkt|ts|bus|sysmon|estate|nodes|mesh|streams)"), "telemetry"),
    (re.compile(r"^(research|report|prebaked)"), "research"),
    (re.compile(r"^(code|repo|docs|notes|kb|wiki)"), "docs"),
    (re.compile(r"^(delegate|agents)"), "delegate"),
)
INTENTS = ("none", "recent", "historical")
_RECENT = re.compile(r"\b(latest|newest|today|tonight|yesterday|this (week|month|morning)|right now|currently|current|"
                     r"recent|recently|just now|so far this)\b", re.I)
_HISTORICAL = re.compile(r"\b(originally|first time|at first|in the beginning|history|historical|historically|"
                         r"used to|back then|earliest)\b", re.I)


def klass(dataset_id: str) -> str:
    d = str(dataset_id or "").lower()
    for rx, cls in _CLASS_RULES:
        if rx.search(d):
            return cls
    return "default"


def temporal_intent(query: str) -> str:
    """'recent' | 'historical' | 'none' from the question's words (historical wins a tie: it is the rarer ask)."""
    q = str(query or "")
    if _HISTORICAL.search(q):
        return "historical"
    if _RECENT.search(q):
        return "recent"
    return "none"


def age_days(created_at: Any, now: Optional[datetime] = None) -> Optional[float]:
    s = str(created_at or "").strip()
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - d).total_seconds() / 86400.0)


def time_weight(age: Optional[float], cls: str, intent: str = "none") -> float:
    """0.5 ** (age / half-life), never below FLOOR; 1.0 for an unknown age, a no-decay class or a historical ask."""
    hl = HALF_LIFE.get(cls, HALF_LIFE["default"])
    if age is None or hl is None or intent == "historical":
        return 1.0
    if intent == "recent":
        hl = hl / 2.0
    return max(FLOOR, 0.5 ** (age / hl))


def importance_weight(importance: Any) -> float:
    try:
        v = float(importance)
    except (TypeError, ValueError):
        return 1.0
    return 0.5 + 0.5 * max(0.0, min(1.0, v))


def score(relevance: float, created_at: Any, dataset_id: str, importance: Any = None, intent: str = "none",
          now: Optional[datetime] = None) -> float:
    return float(relevance) * time_weight(age_days(created_at, now), klass(dataset_id), intent) \
        * importance_weight(importance)
