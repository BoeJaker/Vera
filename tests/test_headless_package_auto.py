"""A stopgap: when nobody can answer an approval prompt, install instead of stalling.

Observed 2026-08-27, unattended census: a loop that had just WRITTEN its own
tests reached for pytest, the gate emitted an approval request, waited 300s for
a human who was not there, and failed the step:

    exec.python.run FAILED - No answer within 300s, so these packages were NOT
    installed: pytest (pip) - needed for `import pytest`

policy="ask" protects nothing in that situation - it just converts a missing
package into a five-minute silent stall and then the same failure.

This is a REAL widening of what installs without review, so the tests below care
more about what it must NOT do than about the convenience it buys.

Imports the app module (plugin: orchestrator first), so it runs in-container.
"""
import pytest

try:
    import Vera.vera.capability_orchestration          # noqa: F401
    from Vera.vera.remote import session_sandbox_capabilities as S
except Exception:                                      # pragma: no cover
    S = None

pytestmark = pytest.mark.skipif(S is None, reason="app module not importable here")

REQ = [{"package": "pytest", "kind": "pip", "needed_for": "import pytest"}]


def test_this_module_actually_imported_the_app():
    assert S is not None and hasattr(S, "_pkg_headless_auto")


def test_it_is_off_by_default():
    """A bypass of human approval must never be the default."""
    assert S._pkg_headless_auto({}) is False
    assert S._classify_requests({}, REQ)["ask"], "default must still ASK"


def test_when_on_an_ask_becomes_an_install():
    split = S._classify_requests({"package_headless_auto": True}, REQ)
    assert split["ask"] == [], "nothing should be left waiting for a human"
    assert len(split["auto"]) == 1


def test_the_bypass_is_marked_so_it_is_visible_afterwards():
    """An install authorised this way must be distinguishable from a normal one."""
    split = S._classify_requests({"package_headless_auto": True}, REQ)
    assert split["auto"][0].get("headless_auto") is True


def test_the_blocklist_still_wins():
    """The flag converts ASK. It must not override an explicit refusal."""
    cfg = {"package_headless_auto": True, "package_blocklist": ["pytest"]}
    split = S._classify_requests(cfg, REQ)
    assert split["auto"] == [] and split["ask"] == []
    assert len(split["denied"]) == 1


def test_policy_deny_still_wins():
    cfg = {"package_headless_auto": True, "package_policy": "deny"}
    split = S._classify_requests(cfg, REQ)
    assert split["auto"] == [], "deny means deny, headless or not"
    assert len(split["denied"]) == 1


def test_an_allowlisted_package_is_not_marked_as_a_bypass():
    """It was already permitted - the marking must mean something."""
    cfg = {"package_headless_auto": True, "package_allowlist": ["pytest"]}
    split = S._classify_requests(cfg, REQ)
    assert len(split["auto"]) == 1
    assert not split["auto"][0].get("headless_auto")


@pytest.mark.parametrize("val,expected", [
    ("1", True), ("true", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("off", False),
])
def test_the_env_override_works_both_ways(monkeypatch, val, expected):
    """Settable for one process without persisting it into the shared config."""
    monkeypatch.setenv(S._PKG_HEADLESS_AUTO_ENV, val)
    assert S._pkg_headless_auto({"package_headless_auto": not expected}) is expected


def test_an_unset_env_falls_back_to_the_config(monkeypatch):
    monkeypatch.delenv(S._PKG_HEADLESS_AUTO_ENV, raising=False)
    assert S._pkg_headless_auto({"package_headless_auto": True}) is True
    assert S._pkg_headless_auto({"package_headless_auto": False}) is False
