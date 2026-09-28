"""Which Ollama request errors count against a NODE's health.

A node is marked offline after three failed requests (`inst["errors"] >= 3`),
and stays out of rotation until the 20 s health ping clears it. That budget
exists for transport faults: timeouts, refused connections, 5xx. It was also
charged for a 404 `model 'x' not found` - the caller's mistake, answered by a
healthy node in a millisecond.

2026-09-22, census run59, `author-then-edit`: the executor asked the web
operator for `provider: "...:fast-preview"`, a model it made up. Three thinks
404'd inside two seconds, gpu-250 went offline on the third, and the very next
executor call (35 k chars, pinned to the GPU) was routed "no GPU has model" to
cpu-246, where a 9b model ran at 0.1 tok/s for 2,740 s. Every embed routed to
cpu-246 queued behind it and timed out at 300 s (9 of research-report's 26
embed calls; 2,550 of its 2,600 embed seconds). One invented word cost two
goals and looked like an "embedding storm".

So: a model-not-found answer is never a node fault. Everything else still is.
"""
from typing import Any


def is_model_not_found(err: Any) -> bool:
    """Ollama's 404 for an unknown model, as Vera surfaces it (raw
    `model 'x' not found`, the wrapped `ollama returned 404: {...}`, or a
    dict with that text under `error`)."""
    if isinstance(err, dict):
        err = err.get("error") or ""
    s = str(err or "").lower()
    return "not found" in s and "model" in s


def is_node_fault(err: Any) -> bool:
    """True when a failed request should count toward the node's error
    budget. A healthy node that correctly refuses an unknown model has not
    faulted; charging it takes a good GPU out of rotation for the next 20 s
    and spills whatever comes next onto a CPU node."""
    if not str(err or "").strip() and not isinstance(err, dict):
        return True   # message-less transport errors are node faults
    return not is_model_not_found(err)
