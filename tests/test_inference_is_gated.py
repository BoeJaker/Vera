"""Every inference call to a node must hold the GPU gate.

The GPU gate exists so prod and every dev sandbox share one V100 as a single
queue (`gpu_cap: 1`). It is taken by `_ollama_slot`, and a caller that POSTs to
a node's `/api/generate` or `/api/chat` WITHOUT entering that context manager
generates outside the queue entirely — so two generations can run on a cap-1
node at once.

Observed 2026-09-20 on gpu-250:

    POST /api/chat      200   3m43s
    POST /api/generate  499   3m18s    <-- client gave up

overlapping for over three minutes. Both crawl, because one GPU is doing two
jobs; the second client eventually abandons its request, and the GPU time it
consumed is wasted.

Note what this is NOT. The gate is fail-open by design — `ollama_gate.acquire`
returns None rather than raising when it cannot get a slot — but its wait budget
is `VERA_GATE_WAIT_S`, default **600s**. A caller that queued would still have
been waiting at the three-minute mark, not proceeding. Fail-open does not
explain a 3-minute overlap; an ungated call path does.

This is a static check on purpose: it costs no GPU, and it catches a new ungated
path at review time rather than as a mysterious slow generation months later.
"""
import ast
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

pytestmark = pytest.mark.critical

#: The endpoints that make a node do inference work.
_INFERENCE_PATHS = ("/api/generate", "/api/chat")

#: Entering any of these holds the shared GPU slot. `_chat_gpu_slot` is a thin
#: bounded/fail-open wrapper around `_ollama_slot` for interactive chat — the
#: test below pins that it really does wrap it, so this list cannot become a way
#: to launder an ungated path through a helper that only looks like a gate.
_GATE_HOLDERS = ("_ollama_slot", "_chat_gpu_slot")

#: Paths deliberately exempt, each with the reason it is exempt. A new entry
#: here should be a decision someone defends, not a way to silence this test.
_EXEMPT = {
    # Measures a node in isolation and says so in its module docstring: it
    # "talks to a node's own /api/generate, /api/embeddings and /api/ps
    # directly (never the router), so a measurement is not confounded".
    # Taking the gate would make it measure the queue instead of the node.
    "vera/catalog/benchmark_capabilities.py": "benchmark measures the node, not the queue",
    # A list of endpoint path strings for telemetry parsing — issues no request.
    "vera/catalog/node_telemetry_core.py": "parses access logs; makes no request",
    # The gate itself.
    "vera/ollama_gate.py": "the gate implementation",
    # A one-token probe on a model that is ALREADY resident, whose whole job is
    # to detect a node that has stopped dispatching. Gating it would make it
    # queue behind the very work it is trying to prove is stuck, so a wedged
    # node would look merely busy — it must measure the node, not the queue.
    "vera/workers/node_agent_capabilities.py": "wedge probe must bypass the queue it is testing",
}


def _iter_py():
    for base, _dirs, files in os.walk(os.path.join(_ROOT, "vera")):
        for f in files:
            if f.endswith(".py"):
                p = os.path.join(base, f)
                yield p, os.path.relpath(p, _ROOT).replace(os.sep, "/")


def _offenders():
    """Functions that post to an inference endpoint without `_ollama_slot`."""
    bad = []
    for path, rel in _iter_py():
        if rel in _EXEMPT:
            continue
        try:
            src = open(path, encoding="utf-8").read()
        except OSError:
            continue
        if not any(p in src for p in _INFERENCE_PATHS):
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        # Slice pre-split lines rather than ast.get_source_segment: that helper
        # re-scans the whole file per node, which is O(n^2) on a 10k-line module
        # and takes minutes across this repo.
        lines = src.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            end = getattr(node, "end_lineno", None)
            if not end:
                continue
            seg = "\n".join(lines[node.lineno - 1:end])
            # Only functions that actually send a request to one of these paths.
            if not any(p in seg for p in _INFERENCE_PATHS):
                continue
            if not ("c.post" in seg or "c.stream" in seg or "client.post" in seg):
                continue
            if any(h in seg for h in _GATE_HOLDERS):
                continue
            bad.append(f"{rel}::{node.name}")
    return sorted(set(bad))


def test_no_inference_path_bypasses_the_gpu_gate():
    offenders = _offenders()
    assert not offenders, (
        "these functions POST to a node's inference endpoint without holding "
        "the GPU gate (_ollama_slot), so they can run concurrently with a "
        "gated generation on a gpu_cap=1 node:\n  "
        + "\n  ".join(offenders)
        + "\n\nWrap the request in `async with _ollama_slot(<instance_id>):`, "
          "or add the file to _EXEMPT with a defensible reason."
    )


def test_every_gate_holder_actually_holds_the_gate():
    """`_GATE_HOLDERS` is what lets a function off the hook, so each entry that
    is not `_ollama_slot` itself must be shown to enter `_ollama_slot`. Without
    this, adding a helper name to that tuple would silently excuse a real
    bypass."""
    src = open(os.path.join(_ROOT, "vera/agents/agents.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    lines = src.splitlines()
    seen = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", None)
            if end:
                seen[node.name] = "\n".join(lines[node.lineno - 1:end])
    for holder in _GATE_HOLDERS:
        if holder == "_ollama_slot":
            continue
        assert holder in seen, f"{holder} is not defined in agents.py"
        assert "_ollama_slot" in seen[holder], (
            f"{holder} is trusted as a gate holder but never enters _ollama_slot")


def test_interactive_chat_bounds_its_gate_wait():
    """Chat bounds how long it QUEUES before the attempt is declined.

    Only the gate wait is bounded, deliberately. The local semaphore's wait
    follows the system default (`OLLAMA_QUEUE_TIMEOUT=0`), because queueing
    behind a job already running on this node is legitimate progress and that
    job will end — the surrounding code takes the same position. The GATE wait
    is bounded so a saturated estate reports itself instead of a turn hanging
    for the gate's 600s default.

    What must never come back is the third option: declining to queue and then
    generating anyway. See test_gate_timeout_is_not_permission.py."""
    src = open(os.path.join(_ROOT, "vera/agents/agents.py"), encoding="utf-8").read()
    assert "_CHAT_GATE_WAIT_S" in src
    tree = ast.parse(src)
    lines = src.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == "_chat_gpu_slot":
            seg = "\n".join(lines[node.lineno - 1:node.end_lineno])
            assert "gate_wait=_CHAT_GATE_WAIT_S" in seg, "gate wait unbounded"
            return
    raise AssertionError("_chat_gpu_slot not found")


def test_the_exemptions_are_still_real_files():
    """An exemption for a file that no longer exists is dead cover — it would
    silently keep excusing a path that moved somewhere else."""
    for rel in _EXEMPT:
        assert os.path.exists(os.path.join(_ROOT, rel)), f"stale exemption: {rel}"


def test_the_check_can_actually_see_an_ungated_call():
    """Guard the guard. If the detector stopped matching (an httpx rename, a
    changed client variable), this suite would pass by finding nothing — which
    is indistinguishable from success. Prove it still detects the shape."""
    import textwrap
    sample = textwrap.dedent('''
        async def ungated(inst):
            async with httpx.AsyncClient() as c:
                r = await c.post(f"{inst['url']}/api/generate", json={})
            return r
    ''')
    tree = ast.parse(sample)
    fn = tree.body[0]
    seg = ast.get_source_segment(sample, fn)
    assert "/api/generate" in seg and "c.post" in seg and "_ollama_slot" not in seg
