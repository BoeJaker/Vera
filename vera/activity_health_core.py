"""/health's `activity`: is anyone running loops, gates, tests or probes on this box. Pure.

4 Oct 2026: a session asked whether a census was running; /health only knew the census, so
a probe batch, its agent loop and three queued gates were invisible - and releases restarted
prod under them. `activity` lists what is in flight and says it in one line.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

#: Declared-activity records live at this key prefix (sys.activity.declare), with a TTL.
KEY_PREFIX = "vera:activity:declared:"
MAX_TTL_S = 6 * 3600
DEFAULT_TTL_S = 1800
KINDS = ("probe", "test", "benchmark", "census", "experiment", "maintenance", "other")


def summarise(loops: Iterable[Dict[str, Any]], gates: Iterable[Dict[str, Any]],
              declared: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    loops, gates, declared = list(loops or []), list(gates or []), list(declared or [])
    parts: List[str] = []
    for d in declared[:3]:
        parts.append("%s %s%s" % (d.get("kind") or "activity", d.get("label") or d.get("id") or "",
                                  (" (%s)" % d["by"]) if d.get("by") else ""))
    if loops:
        parts.append("%d agent loop%s" % (len(loops), "" if len(loops) == 1 else "s"))
    running = [g for g in gates if g.get("state") == "running"]
    queued = [g for g in gates if g.get("state") != "running"]
    if running:
        parts.append("gate running: %s" % ", ".join(str(g.get("branch") or g.get("id")) for g in running[:2]))
    if queued:
        parts.append("%d gate%s queued" % (len(queued), "" if len(queued) == 1 else "s"))
    return {"busy": bool(loops or gates or declared),
            "summary": " · ".join(parts) if parts else "idle",
            "loops": loops[:10], "gates": gates[:10], "declared": declared[:10]}


def clamp_ttl(ttl_s: Any) -> int:
    try:
        t = int(ttl_s)
    except (TypeError, ValueError):
        t = DEFAULT_TTL_S
    return max(60, min(MAX_TTL_S, t))


def norm_kind(kind: Any) -> str:
    k = str(kind or "").strip().lower()
    return k if k in KINDS else "other"
