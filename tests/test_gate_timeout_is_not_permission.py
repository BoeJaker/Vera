"""Waiting out the queue is not permission to generate anyway.

The cross-process GPU gate used to fail open on every reason it could decline.
A caller that queued for the full budget (`VERA_GATE_WAIT_S`, default 600s) then
generated regardless — which is exactly the flooding the gate exists to prevent,
and the barge-in was invisible because it was logged at debug.

The reasons are not equivalent and must not be treated alike:

    queue_timeout        the node is busy. Decline. The caller's normal error
                         path handles it, as it already does for the LOCAL
                         queue timeout.
    coordination_error   Redis threw; the gate itself is broken. Proceed, but
                         loudly — failing all inference because the coordinator
                         is down is worse than running ungated, and that is an
                         availability trade-off, not a queueing decision.
    ungated_node         the node has no gate (capacity 0). Proceeding is
                         correct; there is nothing to queue for.

The first two tests exercise the real `acquire` against a fake Redis, so they
check behaviour rather than wording. The rest are static checks on the call
sites, because the interesting part — whether the caller HONOURS a refusal —
cannot be reached without booting the orchestrator.
"""
import ast
import asyncio
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from vera import ollama_gate as G      # noqa: E402

pytestmark = pytest.mark.critical


class _FullRedis:
    """Every slot is taken: `set(nx=True)` always declines."""

    async def set(self, *_a, **_kw):
        return None


class _BrokenRedis:
    """Redis itself is failing."""

    async def set(self, *_a, **_kw):
        raise RuntimeError("connection reset by peer  password=hunter2")


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── The behaviour that was wrong ─────────────────────────────────────────────

def test_a_full_queue_refuses_instead_of_returning_none():
    """THE fix: required=True turns 'waited long enough' into a refusal."""
    with pytest.raises(G.GateAcquisitionError) as e:
        _run(G.acquire(_FullRedis(), "gpu-250", 1, 1000, wait=0.05,
                       poll=0.01, required=True))
    assert "queue_timeout" in str(e.value)


def test_legacy_callers_still_fail_open():
    """Unchanged for anything that has not opted in — the old contract is what
    the ungated-node and benchmark paths still rely on."""
    got = _run(G.acquire(_FullRedis(), "gpu-250", 1, 1000, wait=0.05,
                         poll=0.01))
    assert got is None


def test_an_ungated_node_is_not_a_refusal():
    """capacity 0 means the node has no gate at all. Refusing there would stop
    inference on every CPU node, which is not what strictness is for."""
    assert _run(G.acquire(_FullRedis(), "cpu-246", 0, 1000, wait=0.01)) is None


def test_a_broken_coordinator_is_distinguishable_from_a_busy_node():
    """Both decline, but the caller must be able to tell them apart — one means
    'wait your turn', the other means 'the gate is down'."""
    with pytest.raises(G.GateAcquisitionError) as e:
        _run(G.acquire(_BrokenRedis(), "gpu-250", 1, 1000, wait=0.05,
                       poll=0.01, required=True))
    assert "coordination_error" in str(e.value)
    assert "queue_timeout" not in str(e.value)


def test_a_transport_error_never_leaks_credentials():
    """The reason is a fixed token, not the exception text — a Redis URL can
    carry a password."""
    with pytest.raises(G.GateAcquisitionError) as e:
        _run(G.acquire(_BrokenRedis(), "gpu-250", 1, 1000, wait=0.05,
                       poll=0.01, required=True))
    assert "hunter2" not in str(e.value)


def test_a_free_slot_is_still_granted():
    """Guard the guard: if acquire refused everything these tests would pass
    for the wrong reason."""
    class _Free:
        async def set(self, *_a, **_kw):
            return True
    lease = _run(G.acquire(_Free(), "gpu-250", 1, 1000, wait=0.05,
                           poll=0.01, required=True))
    assert lease and lease.get("node") == "gpu-250"


# ── The call sites must honour a refusal ─────────────────────────────────────

def _fn_source(rel, name):
    src = open(os.path.join(_ROOT, rel), encoding="utf-8").read()
    lines = src.splitlines()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return "\n".join(lines[n.lineno - 1:n.end_lineno])
    raise AssertionError(f"{name} not found in {rel}")


def test_the_slot_asks_for_a_real_refusal():
    seg = _fn_source("vera/capability_orchestration.py", "_ollama_slot")
    assert "required=True" in seg, (
        "_ollama_slot still calls acquire in legacy fail-open mode, so a queue "
        "timeout silently becomes permission to generate")
    assert "queue_timeout" in seg, "the two refusal reasons are not distinguished"


def test_a_queue_timeout_is_raised_not_logged_away():
    seg = _fn_source("vera/capability_orchestration.py", "_ollama_slot")
    # The queue-timeout branch must raise; only the coordination branch may
    # fall through to proceeding.
    assert "raise Exception(" in seg
    assert "log.warning" in seg, (
        "proceeding ungated because coordination is down must be visible, not "
        "swallowed at debug")


def test_refusing_the_gate_still_hands_back_the_local_permit():
    """The worst possible way to get this wrong.

    `_ollama_slot` holds a per-node semaphore permit BEFORE it tries the shared
    gate. Raising out of the gate block without reaching the finally would leak
    that permit — and it is the node's only one, so every later request on that
    node would block forever. The surrounding code already carries a comment
    about exactly this leak, from a previous occurrence.

    So: the gate-refusal raise must sit inside the try whose finally calls
    sem.release().
    """
    src = open(os.path.join(_ROOT, "vera/capability_orchestration.py"),
               encoding="utf-8").read()
    lines = src.splitlines()
    fn = None
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and n.name == "_ollama_slot":
            fn = n
    assert fn is not None

    def body_text(stmts):
        out = []
        for st in stmts:
            out.extend(lines[st.lineno - 1:(st.end_lineno or st.lineno)])
        return "\n".join(out)

    # The gate refusal is the raise that mentions the gate.
    gate_raises = [n.lineno for n in ast.walk(fn)
                   if isinstance(n, ast.Raise)
                   and "gpu gate timeout" in body_text([n])]
    assert gate_raises, "no gate-refusal raise found in _ollama_slot"

    for rl in gate_raises:
        released = any(
            isinstance(n, ast.Try) and n.finalbody
            and n.lineno <= rl <= (n.end_lineno or 0)
            and "sem.release()" in body_text(n.finalbody)
            for n in ast.walk(fn))
        assert released, (
            f"the gate refusal at line {rl} escapes the try whose finally "
            f"releases the node's semaphore permit — that permit would leak "
            f"and block every later request on the node")


def test_chat_does_not_generate_after_being_refused():
    """The barge-in, moved behind a shorter timer, is still a barge-in."""
    seg = _fn_source("vera/agents/agents.py", "run_stream")
    assert "_gate_cm.__aenter__()" in seg
    # After the except, the generator must stop rather than continue into the
    # request. A bare `_gate_cm = None` followed by the normal path is the bug.
    after = seg.split("_gate_cm.__aenter__()", 1)[1]
    handler = after.split("_chat_slot", 1)[0]
    assert "return" in handler, (
        "run_stream swallows a gate refusal and carries on to /api/chat — that "
        "is the ungated generation this change removes")


def test_the_chat_helper_does_not_swallow_a_refusal():
    seg = _fn_source("vera/agents/agents.py", "_chat_gpu_slot")
    assert "_ollama_slot" in seg
    assert "except Exception" not in seg, (
        "_chat_gpu_slot catches the refusal and yields anyway, so chat would "
        "generate unslotted")
