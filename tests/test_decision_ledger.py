"""The decision ledger (four-minds FM-0.2): v0 row rules, the stream write path and the drainer."""
import asyncio
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import ledger_core as LC  # noqa: E402

pytestmark = pytest.mark.critical


def _dec(**kw):
    base = dict(origin="census", surface="loop", qtype="binary", labels=["continue", "stop"],
                state={"step": 3}, state_ref={"kind": "step", "id": "s1"}, decider="llm",
                implementation="qwen3.5:9b", mode="llm_only", run_id="run1")
    base.update(kw)
    return LC.make_decision("loop.controller_action", "continue", **base)


def test_minted_rows_are_valid():
    d = _dec()
    assert LC.check_decision(d) == []
    assert d["decision_id"].startswith("dec_") and d["labels_hash"] == LC.labels_hash(["stop", "continue"])
    o = LC.make_outcome(d["decision_id"], "correct", "gate.verdict", "strong")
    assert LC.check_outcome(o) == []


def test_bad_rows_are_refused_with_reasons():
    d = _dec()
    d["origin"] = "elsewhere"
    d["p"] = 3.2
    d["labels_hash"] = "x"
    d["state_ref"]["blob"] = "x" * 1000
    errs = " | ".join(LC.check_decision(d))
    for frag in ("origin", "p is not null or 0..1", "labels_hash", "state_ref.blob longer than"):
        assert frag in errs
    assert LC.check_decision("nope") == ["not an object"]
    o = LC.make_outcome("dec_bad", "correct", "x", "huge")
    errs = " | ".join(LC.check_outcome(o))
    assert "decision_id" in errs and "strength" in errs


def test_intel_rows_import_unchanged():
    try:
        from vera.intel import intel_ledger_core as ILC
    except Exception:
        pytest.skip("intel ledger core not present")
    row = ILC.make_decision("intel.signal_relevant", "relevant", state={"t": "x"},
                            state_ref={"kind": "signal", "id": "sig1"}, detail={"score": 0.71})
    assert LC.check_decision(row) == []


def test_partition_and_strongest():
    assert LC.partition_for("2026-12-31T23:00:00Z") == {"name": "decision_ledger_y2026m12",
                                                         "from": "2026-12-01", "to": "2027-01-01"}
    outs = [{"strength": "medium", "ts": "2026-10-04T01:00:00Z", "label": "a"},
            {"strength": "strong", "ts": "2026-10-04T00:00:00Z", "label": "b"},
            {"strength": "strong", "ts": "2026-10-04T02:00:00Z", "label": "c"}]
    assert LC.strongest(outs)["label"] == "c" and LC.strongest([]) is None


# ── write path + drainer against fakes ──────────────────────────────────────

class FakeRedis:
    def __init__(self):
        self.entries, self.acked, self.delivered, self.n = [], set(), set(), 0

    async def xadd(self, stream, fields, maxlen=None, approximate=True):
        if stream != "vera:decide:ledger":      # the orchestrator's own event streams
            return
        self.n += 1
        self.entries.append((f"{self.n}-0", dict(fields)))

    async def xgroup_create(self, *a, **k):
        if getattr(self, "_grp", False):
            raise Exception("BUSYGROUP Consumer Group name already exists")
        self._grp = True

    async def xreadgroup(self, group, consumer, streams, count=10):
        start = list(streams.values())[0]
        if start == "0":
            out = [e for e in self.entries if e[0] in self.delivered and e[0] not in self.acked]
        else:
            out = [e for e in self.entries if e[0] not in self.delivered][:count]
            self.delivered.update(e[0] for e in out)
        return [["vera:decide:ledger", out]] if out else []

    async def xack(self, stream, group, *ids):
        self.acked.update(ids)


class FakeConn:
    def __init__(self, store, fail=False):
        self.store, self.fail = store, fail

    async def execute(self, sql, *args):
        if self.fail:
            raise RuntimeError("pg down")
        self.store.setdefault("sql", []).append(sql.split("(")[0].strip())
        if "INSERT INTO decision_ledger" in sql:
            self.store.setdefault("decisions", {})[args[0]] = json.loads(args[-1])
        elif "INSERT INTO decision_outcomes" in sql:
            self.store.setdefault("outcomes", []).append(json.loads(args[-1]))


class FakePool:
    def __init__(self, store, fail=False):
        self.store, self.fail = store, fail

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return FakeConn(pool.store, pool.fail)

            async def __aexit__(self, *a):
                return False
        return _Ctx()


@pytest.fixture
def ledger(monkeypatch):
    from vera.minds import ledger_capabilities as L
    redis, store = FakeRedis(), {}
    monkeypatch.setattr(L._orch, "REDIS", redis, raising=False)
    monkeypatch.setattr(L._orch, "PG_POOL", FakePool(store), raising=False)
    monkeypatch.setattr(L, "_DDL_DONE", False)
    monkeypatch.setattr(L, "_PARTITIONS", set())
    return L, redis, store


def test_append_then_drain_moves_rows_to_postgres(ledger):
    L, redis, store = ledger
    d = _dec()
    assert asyncio.run(L.append(d))["ok"]
    assert asyncio.run(L.append(LC.make_outcome(d["decision_id"], "correct", "gate.verdict", "strong")))["ok"]
    assert not asyncio.run(L.append({"schema": "vera.decision/v0"}))["ok"]
    assert len(redis.entries) == 2
    res = asyncio.run(L.drain_once())
    assert res == {"ok": True, "moved": 2}
    assert d["decision_id"] in store["decisions"] and store["outcomes"][0]["label"] == "correct"
    assert any("PARTITION OF" in s or s.startswith("CREATE TABLE IF NOT EXISTS decision_ledger_y")
               for s in store["sql"])
    assert redis.acked == {"1-0", "2-0"}


def test_rows_stay_in_the_stream_while_postgres_is_down(ledger, monkeypatch):
    L, redis, store = ledger
    asyncio.run(L.append(_dec()))
    monkeypatch.setattr(L._orch, "PG_POOL", FakePool(store, fail=True), raising=False)
    with pytest.raises(RuntimeError):
        asyncio.run(L.drain_once())
    assert redis.acked == set()
    monkeypatch.setattr(L._orch, "PG_POOL", FakePool(store), raising=False)
    assert asyncio.run(L.drain_once())["moved"] == 1      # redelivered from the pending list


def test_import_checks_then_appends(ledger):
    L, redis, _ = ledger
    rows = [_dec(), {"schema": "vera.decision/v0", "decision_id": "dec_x"}]
    dry = asyncio.run(L.cap_decide_ledger_import(rows=rows))
    assert dry["dry_run"] and dry["valid"] == 1 and dry["refused"] == 1 and redis.entries == []
    jsonl = "\n".join(json.dumps(r) for r in rows)
    real = asyncio.run(L.cap_decide_ledger_import(rows=jsonl, dry_run="false"))
    assert real["appended"] == 1 and len(redis.entries) == 1


def test_record_hands_inputs_to_the_training_store(ledger, monkeypatch):
    # four-minds FM-13.6: an emitter outside the loop (intel) passes what a decision saw beside the row
    import types
    L, redis, _ = ledger
    put = []
    monkeypatch.setitem(sys.modules, "training_state", types.SimpleNamespace(put=lambda d, i: put.append((d, i))))
    d = _dec()
    assert asyncio.run(L.cap_decide_record(row=d, inputs={"goal": "g"}))["ok"]
    assert put == [(d, {"goal": "g"})]
    asyncio.run(L.cap_decide_record(row=_dec()))                                    # no inputs: nothing stored
    asyncio.run(L.cap_decide_record(row={"schema": "vera.decision/v0"}, inputs={"goal": "g"}))   # refused row
    o = LC.make_outcome(d["decision_id"], "correct", "gate.verdict", "strong")
    asyncio.run(L.cap_decide_record(row=o, inputs={"goal": "g"}))                   # outcomes carry no inputs
    assert len(put) == 1 and len(redis.entries) == 3
