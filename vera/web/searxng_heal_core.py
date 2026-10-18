"""SearXNG self-healing (shared-planning web-ingress §16). Pure: what to do about the search engine's state.

Twice in ten days the searxng container lost its outbound network while staying "up" (8-10 Oct after a host
restart; 17-18 Oct): every engine answered "timeout", every search came back empty, web.search waited on it and
fell back to DuckDuckGo at four times the latency, and nothing acted. A container restart fixed it both times.

search_engines.searxng_status already tells the states apart. This module decides, from that status and the healer's
own history:
  probe     - nobody has searched for a while: ask SearXNG a known query so detection does not wait for traffic;
  restart   - the dead-network signature (every engine times out, or SearXNG unreachable) - and only that: engines
              that answer CAPTCHA / too-many-requests are rate-limited, and a restart does not help;
  escalate  - restarts did not fix it (or the daily budget is spent): say so, once per ESCALATE_EVERY_S;
  none      - everything else (with why).
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

PROBE_QUERY = "wikipedia"
PROBE_EVERY_S = 300            # the tick
PROBE_IF_QUIET_S = 600         # probe only when no search was noted for this long (organic traffic is the probe)
RESTART_COOLDOWN_S = 1800      # never two restarts within 30 minutes
MAX_RESTARTS_PER_DAY = 4
ESCALATE_AFTER_FAILED = 2      # restarts in a row that did not bring it back
ESCALATE_EVERY_S = 6 * 3600
DAY_S = 86400
HISTORY = 20

RATE_LIMIT_WORDS = ("captcha", "too many requests", "access denied", "suspended: access")


def signature(status: Mapping[str, Any]) -> str:
    """dead_network | unreachable | rate_limited | degraded | ok | unknown, from searxng_status's answer."""
    st = str(status.get("state") or "unknown")
    unresp = [str(u).lower() for u in (status.get("unresponsive") or [])]
    if st == "down":
        return "unreachable" if status.get("reachable") is False else "dead_network"
    if st == "degraded":
        if unresp and all(any(w in u for w in RATE_LIMIT_WORDS) for u in unresp):
            return "rate_limited"
        return "degraded"
    return st if st in ("ok", "unknown") else "degraded"


def should_probe(now: float, last_probe_at: Optional[float], last_search_at: Optional[float]) -> bool:
    """A probe when nothing has searched for PROBE_IF_QUIET_S and the last probe is at least a tick old."""
    if last_search_at is not None and now - last_search_at < PROBE_IF_QUIET_S:
        return False
    return last_probe_at is None or now - last_probe_at >= PROBE_EVERY_S - 5


def decide(now: float, status: Mapping[str, Any], heal: Mapping[str, Any]) -> Dict[str, str]:
    """{action: none|restart|escalate, why} for the current status and the healer's history `heal`
    ({restarts: [ts], failed_streak: int, last_escalation: ts})."""
    sig = signature(status)
    if sig not in ("dead_network", "unreachable"):
        return {"action": "none", "why": f"state {sig}" + (" - a restart does not help" if sig == "rate_limited" else "")}
    restarts: List[float] = [float(t) for t in (heal.get("restarts") or [])]
    today = [t for t in restarts if now - t < DAY_S]
    last = max(restarts) if restarts else None
    failed = int(heal.get("failed_streak") or 0)
    if failed >= ESCALATE_AFTER_FAILED or len(today) >= MAX_RESTARTS_PER_DAY:
        le = heal.get("last_escalation")
        if le is None or now - float(le) >= ESCALATE_EVERY_S:
            why = (f"{failed} restart(s) in a row did not bring it back" if failed >= ESCALATE_AFTER_FAILED
                   else f"{len(today)} restarts in 24 h")
            return {"action": "escalate", "why": f"{sig}: {why} - check the docker network / the host's egress"}
        return {"action": "none", "why": f"{sig}: escalated already; next reminder in "
                                         f"{int(ESCALATE_EVERY_S - (now - float(le)))} s"}
    if last is not None and now - last < RESTART_COOLDOWN_S:
        return {"action": "none", "why": f"{sig}: restarted {int(now - last)} s ago; cooling down"}
    return {"action": "restart", "why": f"{sig}: every engine times out" if sig == "dead_network"
            else f"{sig}: SearXNG cannot be reached"}


def after_restart(heal: Mapping[str, Any], now: float, recovered: bool, note: str = "") -> Dict[str, Any]:
    h = dict(heal or {})
    h["restarts"] = ([float(t) for t in (h.get("restarts") or []) if now - float(t) < DAY_S] + [now])[-HISTORY:]
    h["failed_streak"] = 0 if recovered else int(h.get("failed_streak") or 0) + 1
    h["history"] = (list(h.get("history") or []) + [{"ts": now, "event": "restart", "recovered": bool(recovered),
                                                     "note": note[:160]}])[-HISTORY:]
    return h


def after_escalation(heal: Mapping[str, Any], now: float, why: str) -> Dict[str, Any]:
    h = dict(heal or {})
    h["last_escalation"] = now
    h["history"] = (list(h.get("history") or []) + [{"ts": now, "event": "escalate", "note": why[:160]}])[-HISTORY:]
    return h


def on_healthy(heal: Mapping[str, Any]) -> Dict[str, Any]:
    """A healthy answer clears the failure streak (the restart history stays for the daily budget)."""
    h = dict(heal or {})
    if h.get("failed_streak") or h.get("last_escalation"):
        h["failed_streak"] = 0
        h["last_escalation"] = None
    return h
