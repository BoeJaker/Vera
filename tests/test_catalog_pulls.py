"""Model-download bookkeeping across a Vera restart.

A pull is a streaming ``/api/pull`` request owned by *this* Vera process, so a
restart kills the transfer. What used to survive was the Redis mirror, still
claiming ``downloading`` with a frozen speed and ETA — a phantom row the UI
counted as active and offered no way to cancel or dismiss. These tests pin the
staleness rules that retire such records.

Pure-unit: the helpers are loaded straight out of the module source so the suite
does not need fastapi/redis installed.
"""

import os
from datetime import datetime, timedelta, timezone

import pytest

_MOD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "vera", "catalog", "catalog_capabilities.py")


@pytest.fixture(scope="module")
def helpers():
    """The pull staleness helpers, exec'd without importing the whole stack."""
    src = open(_MOD, encoding="utf-8").read()
    start, end = src.index("_ACTIVE_STATES = ("), src.index("async def _pull_all_redis")
    preamble = ("from datetime import datetime, timezone\n"
                "def now_iso():\n"
                "    return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')\n")
    ns: dict = {}
    exec(compile(preamble + src[start:end], _MOD, "exec"), ns)
    return ns


def _ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat().replace("+00:00", "Z")


def _rec(state="downloading", age_s=1, **kw) -> dict:
    r = {"id": "cpu-246-1785683435-1", "model": "hf.co/unsloth/Qwen3.6-35B-A3B-GGUF:Q8_K_XL",
         "instance_id": "cpu-246", "state": state, "error": "",
         "started_at": _ago(hours=2), "updated_at": _ago(seconds=age_s),
         "speed_bps": 10515344, "eta_s": 2052}
    r.update(kw)
    return r


# ── staleness ───────────────────────────────────────────────────────────────
def test_live_pull_is_not_stale(helpers):
    assert not helpers["_pull_is_stale"](_rec(age_s=1))


def test_slow_manifest_fetch_is_not_stale(helpers):
    """No bytes yet is normal for a big repo — don't reap a pull that's starting."""
    assert not helpers["_pull_is_stale"](_rec(state="starting", age_s=90))


def test_orphan_from_a_restart_is_stale(helpers):
    assert helpers["_pull_is_stale"](_rec(age_s=3600))


def test_verifying_orphan_is_stale(helpers):
    assert helpers["_pull_is_stale"](_rec(state="verifying", age_s=3600))


@pytest.mark.parametrize("state", ["done", "failed", "cancelled", "interrupted"])
def test_terminal_records_are_never_stale(helpers, state):
    """Staleness is about a missing owner, not about age — finished is finished."""
    assert not helpers["_pull_is_stale"](_rec(state=state, updated_at="2020-01-01T00:00:00Z"))


def test_unreadable_timestamp_counts_as_dead(helpers):
    assert helpers["_pull_is_stale"](_rec(updated_at="not-a-date", started_at=""))


# ── retirement ──────────────────────────────────────────────────────────────
def test_interrupting_zeroes_the_fake_live_rate(helpers):
    """The frozen speed/ETA were the misleading part: they read as current."""
    rec = helpers["_pull_mark_interrupted"](_rec(age_s=3600))
    assert rec["state"] == "interrupted"
    assert rec["ok"] is False
    assert rec["speed_bps"] == 0 and rec["eta_s"] is None
    assert rec["finished_at"] and rec["error"]


def test_interrupted_record_is_not_reaped_again(helpers):
    rec = helpers["_pull_mark_interrupted"](_rec(age_s=3600))
    assert not helpers["_pull_is_stale"](rec)


def test_interrupting_keeps_a_real_error(helpers):
    rec = helpers["_pull_mark_interrupted"](_rec(age_s=3600, error="no space left on device"))
    assert rec["error"] == "no space left on device"


def test_interrupted_is_not_an_active_state(helpers):
    """Drives the UI: active rows get Cancel, the rest get Resume + Dismiss."""
    assert "interrupted" not in helpers["_ACTIVE_STATES"]


def test_progress_so_far_is_preserved_for_resume(helpers):
    """Ollama keeps the partial blob; the record must keep the byte counts that
    tell the operator resuming is worth it."""
    rec = helpers["_pull_mark_interrupted"](
        _rec(age_s=3600, completed_bytes=16880784751, total_bytes=38451182560, pct=43.9))
    assert rec["completed_bytes"] == 16880784751 and rec["pct"] == 43.9
