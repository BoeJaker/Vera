"""A dev sandbox is a guest. It does not get to promote to main.

Measured inside a live dev sandbox on 2026-08-31: it registers 2261
capabilities - every one prod has - including

    evolve.bleeding_edge.promote_to_main   merges into PROD'S LIVE CHECKOUT
    evolve.branch.delete                   deletes shared git branches
    evolve.sandbox.prune / .down / .reap   reaps the shared sandbox estate

and it shares prod's COORDINATOR Redis (VERA_COORD_REDIS_DB=0), where the
sandbox pool and GPU gate leases live. VERA_IS_DEV_SANDBOX=1 was set the whole
time - but that flag only gated SCHEDULED jobs. Nothing stopped a loop, an
agent, or a user calling these against the sandbox's own API.

Not hypothetical: during census 20 a prune from outside prod emptied the shared
sandbox pool mid-run, and the operator's fallback then had nothing to choose
from and the goal lost its browser.

The balance this must strike: a sandbox legitimately READS all of this - the
Loop Lab UI inside a sandbox is how you inspect a branch - so only operations
that mutate state OUTSIDE the sandbox are refused.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from vera.evolve.sandbox_estate_guard import (       # noqa: E402
    DENIED_IN_SANDBOX, EXPLICITLY_ALLOWED, is_denied, refusal,
)


# --- what must never happen from inside a sandbox ---------------------------

@pytest.mark.parametrize("cap", [
    "evolve.bleeding_edge.promote_to_main",
    "evolve.pipeline.promote",
    "evolve.pipeline.rollback",
    "evolve.branch.delete",
    "evolve.branch.create",
    "evolve.sandbox.prune",
    "evolve.sandbox.down",
    "evolve.sandbox.reap",
    "content.edit",
])
def test_estate_and_git_mutations_are_refused_in_a_sandbox(cap):
    assert is_denied(cap, in_sandbox=True) is True


def test_the_same_calls_are_untouched_on_prod():
    """This must constrain sandboxes only - prod is where the estate is run."""
    for cap in sorted(DENIED_IN_SANDBOX):
        assert is_denied(cap, in_sandbox=False) is False


# --- what must keep working -------------------------------------------------

@pytest.mark.parametrize("cap", [
    "evolve.sandbox.list", "evolve.sandbox.status", "evolve.sandbox.exec",
    "evolve.sandbox.fs.read", "evolve.sandbox.fs.write", "evolve.sandbox.diff",
    "evolve.git.status", "evolve.git.graph", "evolve.audit.list",
    "evolve.pipeline.list", "evolve.pipeline.diff", "content.status",
])
def test_reading_and_working_inside_the_sandbox_still_works(cap):
    """Over-blocking would break the thing a sandbox is FOR."""
    assert is_denied(cap, in_sandbox=True) is False


def test_ordinary_capabilities_are_not_touched_at_all():
    for cap in ("llm.generate", "code.author", "exec.bash.run", "operator.run",
                "web.fetch", "memory.search"):
        assert is_denied(cap, in_sandbox=True) is False


def test_the_allowed_list_and_denied_list_do_not_overlap():
    assert not (DENIED_IN_SANDBOX & EXPLICITLY_ALLOWED)


# --- the refusal ------------------------------------------------------------

def test_the_refusal_says_where_the_call_belongs():
    """"Denied" without a next move is how a guard gets worked around."""
    r = refusal("evolve.bleeding_edge.promote_to_main")
    assert r["ok"] is False
    assert "evolve.bleeding_edge.promote_to_main" in r["error"]
    assert "prod" in r["error"]
    assert r["refused_by"] == "sandbox_estate_guard"


def test_junk_is_never_denied():
    for junk in ("", None, "   "):
        assert is_denied(junk, in_sandbox=True) is False


# --- the callsite -----------------------------------------------------------

def _orch_src():
    p = os.path.join(os.path.dirname(__file__), "..", "vera",
                     "capability_orchestration.py")
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def test_the_dispatcher_consults_the_guard_before_doing_anything():
    src = _orch_src()
    assert "_estate_guard.is_denied(name, in_sandbox=True)" in src
    guard = src.index("_estate_guard.is_denied")
    tid = src.index('tid     = kw.pop("trace_id"', guard - 4000)
    assert guard < tid, "the guard must run before the call is even traced"


def test_a_missing_guard_is_reported_not_silently_ignored():
    """A safety control that fails OPEN must at least be loud about it."""
    src = _orch_src()
    assert "sandbox estate guard UNAVAILABLE" in src


def test_both_package_spellings_are_attempted():
    """`Vera.vera.*` resolves to the MAIN checkout and `vera.*` to whatever is
    on sys.path - a guard that loads under only one of them is off half the
    time."""
    src = _orch_src()
    assert "Vera.vera.evolve.sandbox_estate_guard" in src
    assert "vera.evolve.sandbox_estate_guard" in src
