"""operator.run must be able to obtain a browser on a busy estate."""
import asyncio, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from vera.operator.target_fallback import pick_sandbox, is_driveable
from vera.operator.targets import ensure_target

MIRROR = {"name": "vera-dev-loop-lab-bleeding-edge-mirror",
          "branch": "loop-lab/bleeding-edge-mirror", "running": True,
          "paused": False, "pinned": True, "owner": "claude_code",
          "url": "http://localhost:8994"}
OTHERS_WIP = {"name": "vera-dev-feat-someone-else", "branch": "feat/someone-else",
              "running": True, "paused": False, "pinned": False,
              "owner": "codex", "url": "http://localhost:8993"}
UNOWNED = {"name": "vera-dev-spare", "branch": "feat/spare", "running": True,
           "paused": False, "pinned": False, "owner": "", "url": "http://localhost:8992"}
PAUSED = {"name": "vera-dev-paused", "branch": "feat/paused", "running": True,
          "paused": True, "pinned": True, "owner": "", "url": "http://localhost:8991"}


# --- what may be chosen -----------------------------------------------------

def test_a_pinned_standing_sandbox_is_preferred():
    assert pick_sandbox([OTHERS_WIP, UNOWNED, MIRROR])["name"] == MIRROR["name"]


def test_another_agents_working_container_is_never_chosen_automatically():
    """The operator CLICKS things - it is not a read-only observer."""
    assert pick_sandbox([OTHERS_WIP]) is None


def test_an_unowned_sandbox_is_acceptable_when_nothing_is_pinned():
    assert pick_sandbox([OTHERS_WIP, UNOWNED])["name"] == UNOWNED["name"]


def test_a_paused_sandbox_is_never_chosen():
    """Unpausing someone's container is a side effect, not a fallback."""
    assert pick_sandbox([PAUSED]) is None
    assert is_driveable(PAUSED) is False


def test_a_stopped_or_urlless_sandbox_is_never_chosen():
    assert pick_sandbox([{**UNOWNED, "running": False}]) is None
    assert pick_sandbox([{**UNOWNED, "url": ""}]) is None


def test_an_empty_estate_yields_nothing_rather_than_guessing():
    assert pick_sandbox([]) is None
    assert pick_sandbox(None) is None


def test_the_choice_is_stable_rather_than_listing_order_dependent():
    a = {**UNOWNED, "name": "aaa"}
    b = {**UNOWNED, "name": "bbb"}
    assert pick_sandbox([b, a])["name"] == pick_sandbox([a, b])["name"] == "aaa"


# --- an explicitly named target is never substituted ------------------------

def test_a_named_branch_matches_exactly():
    assert pick_sandbox([MIRROR, UNOWNED],
                        branch="feat/spare")["name"] == UNOWNED["name"]


def test_a_named_branch_that_is_absent_does_not_fall_back():
    """Substituting a container for a named one would drive the wrong UI."""
    assert pick_sandbox([MIRROR, UNOWNED], branch="feat/not-here") is None


def test_a_named_branch_is_matched_even_when_owned_by_someone_else():
    """Explicit is explicit; the owner guard governs only automatic choice."""
    assert pick_sandbox([OTHERS_WIP], branch="feat/someone-else") is not None


# --- ensure_target end to end ----------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _cap_factory(sandboxes, ensure_error="primary sandbox is occupied by another branch",
                 boot=None):
    calls = []

    async def call_cap(name, **kw):
        calls.append(name)
        if name == "evolve.sandbox.list":
            return {"sandboxes": sandboxes}
        if name == "evolve.sandbox.ensure":
            return {"error": ensure_error} if ensure_error else {"port": 8998}
        if name == "evolve.bleeding_edge.container.ensure":
            return dict(boot) if boot else {}
        return {}
    return call_cap, calls


def test_an_occupied_primary_no_longer_ends_the_browser_step():
    """Census 16: six operator.run calls died here in under a second each.

    CONTRACT CHANGED 2026-09-02: the primary is no longer consulted at all for
    an unnamed browser request, so there is no primary_error to report - the
    run simply goes to its reservation. The guarantee this test exists to
    protect is unchanged and stronger: an occupied primary cannot end a browser
    step, because the step never asks for the primary.
    """
    cap, calls = _cap_factory([MIRROR])
    out = _run(ensure_target({"kind": "sandbox"}, cap))
    assert out["ready"] is True
    assert out["base_url"] == "http://localhost:8994"
    assert "8994" in out["note"]
    assert "evolve.sandbox.ensure" not in calls


def test_the_original_failure_survives_when_no_sandbox_is_safe_to_use():
    cap, _ = _cap_factory([OTHERS_WIP])
    out = _run(ensure_target({"kind": "sandbox"}, cap))
    assert out["ready"] is False
    assert "primary sandbox is occupied" in out["error"]


def test_an_unnamed_browser_step_takes_the_reservation_even_when_the_primary_is_free():
    """REPLACES test_a_working_primary_is_still_used (2026-09-02).

    That test encoded "prefer the primary, fall back to a reservation", which
    made contention the normal path: the primary is a one-owner-at-a-time
    singleton, so on a busy estate an unnamed browser step raced for it and
    usually lost. Census 26's build-browser-verified spent its whole 1800s wall
    cap on that race; runs 16 and 19 died the same way.

    The reservation is not a consolation prize - it is where a browser step
    belongs. Taking it even when the primary happens to be free is the point:
    the outcome stops depending on who else is working right now.
    """
    cap, calls = _cap_factory([MIRROR], ensure_error="")
    out = _run(ensure_target({"kind": "sandbox"}, cap))
    assert out["ready"] is True
    assert out["base_url"] == "http://localhost:8994", "took the primary, not the reservation"
    assert "evolve.sandbox.ensure" not in calls, "contended for the primary singleton"


def test_a_named_branch_that_does_not_exist_still_gets_a_browser():
    """Changed 2026-08-31 after census run 19. Refusing to substitute for a
    branch that HAS NO SANDBOX just fails the step - the named lookup already
    proved it does not exist. An operator.run died in 2.4s on the occupied
    primary while a pinned, running, driveable sandbox sat registered, because
    the model had named a branch and that silently disabled the fallback."""
    cap, _ = _cap_factory([MIRROR])
    out = _run(ensure_target({"kind": "sandbox", "branch": "feat/not-here"}, cap))
    assert out["ready"] is True
    assert out["base_url"] == "http://localhost:8994"
    assert "feat/not-here" in out["note"], "the substitution must say what was asked for"


def test_a_named_branch_that_EXISTS_is_still_matched_exactly():
    """The original guarantee survives: naming a live container gets THAT one."""
    cap, _ = _cap_factory([MIRROR, UNOWNED])
    out = _run(ensure_target({"kind": "sandbox", "branch": "feat/spare"}, cap))
    assert out["ready"] is True and out["base_url"] == "http://localhost:8992"


def test_when_nothing_is_usable_the_error_says_why():
    """The line that was missing: a guard that declines silently cannot be
    debugged. Run 19's failure was indistinguishable from an empty list."""
    cap, _ = _cap_factory([OTHERS_WIP])          # live, but another agent's
    out = _run(ensure_target({"kind": "sandbox"}, cap))
    assert out["ready"] is False
    assert "primary sandbox is occupied" in out["error"]
    assert "another agent" in out["error"] or "owned and unpinned" in out["error"]


def test_an_empty_list_is_reported_as_such_not_as_no_candidate():
    cap, _ = _cap_factory([])
    out = _run(ensure_target({"kind": "sandbox"}, cap))
    assert "list was empty" in out["error"]


def test_an_explicit_url_survives_being_routed_to_a_named_container():
    """kind=sandbox + branch + url used to resolve to a blank start_url, so the
    operator woke on about:blank and went looking for the page (2026-08-27)."""
    cap, _ = _cap_factory([MIRROR])
    out = _run(ensure_target(
        {"kind": "sandbox", "branch": "loop-lab/bleeding-edge-mirror",
         "url": "http://localhost:8994/preview/form.html"}, cap))
    assert out["ready"] is True
    assert out["start_url"] == "http://localhost:8994/preview/form.html"


def test_a_url_target_never_touches_the_sandbox_machinery():
    cap, calls = _cap_factory([MIRROR])
    out = _run(ensure_target({"kind": "url", "url": "https://example.com/x"}, cap))
    assert out["ready"] is True and out["start_url"] == "https://example.com/x"
    assert calls == []


# --- the refusal reason -----------------------------------------------------

def test_decline_reason_names_the_actual_obstacle():
    from vera.operator.target_fallback import decline_reason
    assert "empty" in decline_reason([])
    assert "driveable" in decline_reason([{**UNOWNED, "running": False}])
    assert "another agent" in decline_reason([OTHERS_WIP])
    r = decline_reason([MIRROR], branch="feat/nope")
    assert "feat/nope" in r and "live" in r


def test_decline_reason_never_raises_on_junk():
    from vera.operator.target_fallback import decline_reason
    for junk in (None, [], [{}], [{"name": None}]):
        assert isinstance(decline_reason(junk), str)


# --- the reservation --------------------------------------------------------
#
# "The operator should not contend for a sandbox, it should have a reserved
# one." Contention was not an edge case here: it was the default path.

def test_a_reservation_must_be_pinned_not_merely_free():
    """pick_reserved is deliberately narrower than pick_sandbox's last resort.

    An unpinned container can be reaped or paused out from under a run
    mid-click, so "nobody owns it" is not a reservation.
    """
    from vera.operator.target_fallback import pick_reserved
    assert pick_reserved([MIRROR])["name"] == MIRROR["name"]
    assert pick_reserved([UNOWNED]) is None
    assert pick_reserved([OTHERS_WIP]) is None
    assert pick_reserved([PAUSED]) is None
    assert pick_reserved([]) is None


def test_with_no_reservation_up_one_is_booted_rather_than_taking_the_primary():
    booted = {"name": "vera-dev-loop-lab-bleeding-edge-mirror",
              "branch": "loop-lab/bleeding-edge-mirror",
              "url": "http://localhost:8992", "reachable": True}
    cap, calls = _cap_factory([], ensure_error="", boot=booted)
    out = _run(ensure_target({"kind": "sandbox"}, cap))
    assert out["ready"] is True
    assert out["base_url"] == "http://localhost:8992"
    assert "evolve.bleeding_edge.container.ensure" in calls
    assert "evolve.sandbox.ensure" not in calls, "took the primary instead of booting"


def test_a_reserved_container_that_comes_up_unreachable_is_not_driven():
    """Adopting an unreachable url sends the browser at nothing and the run
    fails later, somewhere less obvious."""
    cap, calls = _cap_factory(
        [], boot={"name": "x", "url": "http://localhost:8992", "reachable": False})
    out = _run(ensure_target({"kind": "sandbox"}, cap))
    assert out["ready"] is False
    assert "evolve.sandbox.ensure" in calls, "should still fall through to the old path"


def test_booting_the_reservation_failing_falls_back_not_crashes():
    """A boot failure must degrade to the previous behaviour, not end the run."""
    async def cap(name, **kw):
        if name == "evolve.sandbox.list":
            return {"sandboxes": [MIRROR]}
        if name == "evolve.bleeding_edge.container.ensure":
            raise RuntimeError("cap not registered here")
        if name == "evolve.sandbox.ensure":
            return {"error": "primary sandbox is occupied by another branch"}
        return {}
    # MIRROR is pinned, so the reservation is found without booting at all
    out = _run(ensure_target({"kind": "sandbox"}, cap))
    assert out["ready"] is True and out["base_url"] == "http://localhost:8994"


def test_a_named_branch_still_bypasses_the_reservation_entirely():
    """Explicit is explicit - naming a container must still drive THAT one,
    reservation or no reservation."""
    cap, _ = _cap_factory([MIRROR, UNOWNED])
    out = _run(ensure_target({"kind": "sandbox", "branch": "feat/spare"}, cap))
    assert out["ready"] is True and out["base_url"] == "http://localhost:8992"


def test_the_note_says_the_primary_was_left_alone():
    """The trace must distinguish 'went where it belongs' from 'lost a race'."""
    cap, _ = _cap_factory([MIRROR])
    out = _run(ensure_target({"kind": "sandbox"}, cap))
    assert "reserved" in out["note"]
    assert "primary" in out["note"]
