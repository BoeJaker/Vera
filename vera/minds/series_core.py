"""Rows for the four-minds history series (Loop Lab Tests timeline, 18 Oct 2026). Pure: no I/O.

Each builder takes the report a capability already returns and keeps the fields a timeline lane draws.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

KINDS = {"matrix": "series.matrix_coverage", "claims": "series.claims_accuracy", "worldview": "series.worldview_spread"}
CLAIMS_Q = "output.claim_supported"
MIN_OUTCOMES = 30                     # the cascade's floor: below it an accuracy is shown, not ranked


def matrix_row(report: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Coverage of the candidate matrix from decide.matrix.report."""
    if not isinstance(report, dict) or report.get("error") or "applications" not in report:
        return None
    rot = report.get("rotation") or {}
    apps = report.get("applications") or {}
    beats = sum(1 for a in apps.values()
                for c in (a.get("candidates") or [])
                if (c.get("outcomes_n") or 0) >= MIN_OUTCOMES and c.get("accuracy") is not None
                and (a.get("live") or {}).get("accuracy") is not None
                and c["accuracy"] > a["live"]["accuracy"])
    return {"pairs_scored": report.get("pairs_scored"), "pairs_untested": report.get("pairs_untested"),
            "applications": len(apps), "candidates": len(report.get("candidates") or []),
            "pairs_beating_live": beats,
            "llm_pairs_total": rot.get("llm_pairs_total"), "queued_ever": rot.get("queued_ever"),
            "budget_per_night": rot.get("budget_per_night")}


def claims_row(report: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Claims accuracy per judge vs the live rule, from decide.matrix.report's output.claim_supported table."""
    app = ((report or {}).get("applications") or {}).get(CLAIMS_Q) if isinstance(report, dict) else None
    if not isinstance(app, dict):
        return None
    live = app.get("live") or {}
    judges = {c["candidate"]: {"accuracy": c.get("accuracy"), "outcomes_n": c.get("outcomes_n") or 0, "n": c.get("n")}
              for c in app.get("candidates") or [] if c.get("candidate")}
    ranked = [(k, v) for k, v in judges.items() if v["accuracy"] is not None and v["outcomes_n"] >= MIN_OUTCOMES]
    best = max(ranked, key=lambda kv: kv[1]["accuracy"]) if ranked else None
    return {"live_accuracy": live.get("accuracy"), "live_n": live.get("n"), "judges": judges,
            "best_judge": best[0] if best else None, "best_accuracy": best[1]["accuracy"] if best else None,
            "beats_live": bool(best and live.get("accuracy") is not None and best[1]["accuracy"] > live["accuracy"])}


def worldview_row(health: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The worldview's spread from worldview.health (FM-W.1 metrics)."""
    if not isinstance(health, dict) or "metrics" not in health:
        return None
    m = health.get("metrics") or {}
    keep = ("median_population", "share_below_min", "concepts_below_min", "largest_share", "largest_population",
            "records_per_concept", "records_assigned", "num_concepts")
    return dict({k: m.get(k) for k in keep}, status=health.get("status"))
