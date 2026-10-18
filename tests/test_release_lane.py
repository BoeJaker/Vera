"""The release lane (backbone B1.0, 18 Oct 2026): sessions submit, the lane batches into one release."""
import os
from datetime import datetime, timedelta, timezone

from vera.evolve import release_lane_core as L

T0 = datetime(2026, 10, 18, 16, 0, tzinfo=timezone.utc)


def _req(rid, minutes, **kw):
    return L.new_request(rid=rid, by=kw.pop("by", "vera-62"), reason=kw.pop("reason", "x"), now=T0 + timedelta(minutes=minutes), **kw)


def test_the_batch_waits_its_window_then_fires_once():
    reqs = [_req("a", 0), _req("b", 4)]
    assert L.should_fire(reqs, T0 + timedelta(minutes=5), None, False)[0] is False      # still batching
    ok, why = L.should_fire(reqs, T0 + timedelta(minutes=10), None, False)
    assert ok and "2 request(s)" in why


def test_nothing_fires_while_a_release_is_in_flight_or_too_soon_after_one():
    reqs = [_req("a", 0)]
    assert L.should_fire(reqs, T0 + timedelta(hours=1), None, True)[0] is False
    last = T0 + timedelta(minutes=5)                                                    # a release finished at :05
    assert L.next_fire_at(reqs, last) == last + timedelta(seconds=L.MIN_GAP_S)
    assert L.should_fire(reqs, T0 + timedelta(minutes=12), last, False)[0] is False


def test_an_urgent_request_goes_without_the_window_or_the_gap():
    reqs = [_req("a", 0, urgent=True)]
    assert L.should_fire(reqs, T0 + timedelta(minutes=1), T0, False)[0] is True


def test_the_batch_waits_as_carefully_as_its_most_careful_request_and_never_forces():
    assert L.batch_mode([_req("a", 0), _req("b", 1, census="finish")]) == "finish"
    assert L.batch_mode([_req("a", 0)]) == "goal"
    assert _req("c", 0, census="force")["census"] == "goal"


def test_carried_requests_record_their_release_and_expire_after_a_day():
    reqs = [_req("a", 0, by="vera-6e", reason="WI-14b"), _req("b", 1)]
    assert L.batch_reason(reqs).startswith("release lane - 2 request(s): vera-6e: WI-14b")
    done = L.mark_carried(reqs, "rel1", T0 + timedelta(minutes=11))
    assert {d["status"] for d in done} == {"carried"} and {d["release_id"] for d in done} == {"rel1"}
    assert L.queued(done) == []
    assert not L.expired(done[0], T0 + timedelta(hours=2)) and L.expired(done[0], T0 + timedelta(days=2))


def test_the_tick_runs_the_lane_only_when_nothing_is_pending_and_without_retaking_the_lock():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vera", "evolve", "release_capabilities.py"), encoding="utf-8").read()
    tick = src[src.index("async def run_tick()"):src.index("async def _tick()")]
    assert "await _lane_step(None)" in tick
    step = src[src.index("async def _lane_step("):src.index("async def run_tick()")]
    assert "_open_release(" in step and "_LOCK" not in step and "cap_release_prod(" not in step
    prod = src[src.index("async def cap_release_prod("):src.index('@capability("evolve.release.status"')]
    assert "return await _open_release(" in prod


def test_the_queued_batch_shows_on_the_schedule_releases_layer():
    from vera.evolve import releases_view_core as RV
    lane = {"queued": [_req("a", 0, by="vera-6e", reason="WI-14b"), _req("b", 2, by="vera-62")],
            "fires_at": (T0 + timedelta(minutes=10)).isoformat(), "why": "batching"}
    ev = RV.lane_events(lane, T0, T0 + timedelta(hours=2), T0)
    assert len(ev) == 1 and ev[0]["kind"] == "release-request" and "2 request(s)" in ev[0]["title"]
    assert "vera-6e: WI-14b" in ev[0]["detail"]
    assert RV.lane_events({"queued": []}, T0, T0 + timedelta(hours=1), T0) == []



def test_the_lane_holds_while_the_latest_full_run_of_bleeding_edge_is_red():
    # vera-d2's review of P0-6: adopt gates run a selection; the nightly full tier proves what a release carries
    rows = [{"branch": "bleeding-edge", "pipeline_id": "abc", "failed": 3, "ts": "t3"},       # an adopt gate: not full
            {"branch": "bleeding-edge", "pipeline_id": "", "failed": 1, "errors": 0, "ts": "t2"},
            {"branch": "bleeding-edge", "pipeline_id": "", "failed": 0, "ts": "t1"}]
    held, why = L.full_tier_hold(rows)
    assert held and "t2" in why and "1 failed" in why
    assert L.full_tier_hold([{"branch": "bleeding-edge", "pipeline_id": "", "failed": 0, "ts": "t4"}] + rows) == (False, "the last full run of bleeding-edge (t4) is green")
    assert L.full_tier_hold([{"branch": "feat/x", "failed": 9}])[0] is False


def test_the_tick_checks_the_full_tier_before_it_fires():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vera", "evolve", "release_capabilities.py"), encoding="utf-8").read()
    step = src[src.index("async def _lane_step("):src.index("async def run_tick()")]
    assert step.index("lane.full_tier_hold(") < step.index("_open_release(")
    assert '"evolve.release.lane.held"' in step
