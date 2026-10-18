"""The release lane (backbone B1.0, owner's yes 18 Oct 2026) - pure.

Every release restarts prod for every session (six on 17 Oct alone, from several sessions). Sessions now SUBMIT a
request instead of releasing; the lane batches what is queued into one release of bleeding-edge - after a short
batching window, never closer than MIN_GAP_S to the last one unless a request is urgent - and the release itself
still waits for census goals, gates and protected runs (release_core.may_release). Each request records the release
that carried it.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

#: seconds the oldest request waits so others can join the batch
BATCH_S = 600
#: seconds between two releases from the lane (an urgent request may go sooner)
MIN_GAP_S = 900
#: a carried / withdrawn request stays visible this long
KEEP_S = 24 * 3600
CENSUS_MODES = ("finish", "goal")          # a lane release never forces; force goes through evolve.release.prod


def _dt(s: Any) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d


def new_request(*, rid: str, by: str, reason: str, now: datetime, census: str = "goal", urgent: bool = False,
                edge_sha: str = "") -> Dict[str, Any]:
    c = str(census or "goal").strip().lower()
    return {"id": rid, "by": str(by or "?")[:60], "reason": str(reason or "")[:300],
            "census": c if c in CENSUS_MODES else "goal", "urgent": bool(urgent), "edge_sha": str(edge_sha or "")[:12],
            "at": now.isoformat(timespec="seconds"), "status": "queued", "release_id": "", "done_at": ""}


def queued(reqs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted([r for r in reqs or [] if r.get("status") == "queued"], key=lambda r: str(r.get("at") or ""))


def batch_mode(reqs: Iterable[Dict[str, Any]]) -> str:
    """The batch waits as carefully as its most careful request: finish if any asked for it, else goal."""
    return "finish" if any(r.get("census") == "finish" for r in reqs or []) else "goal"


def next_fire_at(reqs: Iterable[Dict[str, Any]], last_done_at: Optional[datetime]) -> Optional[datetime]:
    q = queued(reqs)
    if not q:
        return None
    first = _dt(q[0].get("at"))
    if first is None:
        return None
    if any(r.get("urgent") for r in q):
        return first
    t = first + timedelta(seconds=BATCH_S)
    if last_done_at is not None:
        t = max(t, last_done_at + timedelta(seconds=MIN_GAP_S))
    return t


def should_fire(reqs: Iterable[Dict[str, Any]], now: datetime, last_done_at: Optional[datetime],
                release_pending: bool) -> Tuple[bool, str]:
    q = queued(reqs)
    if not q:
        return False, "nothing queued"
    if release_pending:
        return False, "a release is already in flight - the queue goes in the next one"
    t = next_fire_at(q, last_done_at)
    if t is not None and now < t:
        return False, f"batching - the lane releases at {t.isoformat(timespec='minutes')}"
    return True, f"{len(q)} request(s) queued since {q[0].get('at')}"


def batch_reason(reqs: Iterable[Dict[str, Any]]) -> str:
    q = queued(reqs)
    parts = [f"{r.get('by')}: {r.get('reason') or '-'}" for r in q[:6]]
    more = f" (+{len(q) - 6} more)" if len(q) > 6 else ""
    return (f"release lane - {len(q)} request(s): " + "; ".join(parts) + more)[:600]


def mark_carried(reqs: Iterable[Dict[str, Any]], release_id: str, now: datetime) -> List[Dict[str, Any]]:
    out = []
    for r in queued(reqs):
        out.append(dict(r, status="carried", release_id=release_id, done_at=now.isoformat(timespec="seconds")))
    return out


def expired(r: Dict[str, Any], now: datetime) -> bool:
    if r.get("status") == "queued":
        return False
    d = _dt(r.get("done_at") or r.get("at"))
    return d is not None and (now - d).total_seconds() > KEEP_S



#: where the latest FULL critical run of bleeding-edge is kept, apart from the gate-heavy history list (vera-d2)
KEY_FULL_TIER_LAST = "vera:evolve:full_tier:last"


def is_full_run(row: Dict[str, Any]) -> bool:
    """A full critical run of bleeding-edge: no pipeline (an adopt gate always has one), the whole tests/ tree (not a
    targeted file), the critical marker. vera-d2's review: a targeted run must neither hold nor lift the lane."""
    paths = row.get("paths")
    paths = "" if paths is None else (",".join(paths) if isinstance(paths, list) else str(paths)).strip()
    return (str(row.get("branch") or "") == "bleeding-edge" and not row.get("pipeline_id")
            and paths in ("", "tests", "tests/") and str(row.get("markers") or "").strip() == "critical")


def full_tier_hold(last: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    """(hold, why) from the latest full run of bleeding-edge (KEY_FULL_TIER_LAST): the lane holds while it is red.
    Adopt gates run only the tests their change touches (P0-6, owner 18 Oct 2026), so the nightly full critical tier
    is what proves what a release carries. A green full run lifts the hold."""
    if not last or not is_full_run(last):
        return False, "no full run of bleeding-edge on record yet"
    bad = int(last.get("failed") or 0) + int(last.get("errors") or 0)
    if bad:
        return True, ("the last full critical run of bleeding-edge (%s) is red: %s failed, %s errors - fix it "
                      "(a green full run lifts the hold) or withdraw" % (last.get("ts") or "?", last.get("failed") or 0, last.get("errors") or 0))
    return False, "the last full run of bleeding-edge (%s) is green" % (last.get("ts") or "?")
