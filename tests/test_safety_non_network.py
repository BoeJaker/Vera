"""A blank page is not a foreign host.

Census 40, author-then-edit - the run's only wall-cap. A browser session that
has not navigated yet sits on `about:blank`, and host_of parses the SCHEME as
the hostname:

    about:blank                    -> host 'about'
    data:text/html,<p>x            -> host 'data'
    chrome-error://chromewebdata/  -> host 'chromewebdata'

None of those is local and none is in any allowlist, so every mutating action
was refused with

    blocked: host 'about' is not a local/Vera surface and is not in the
    allowlist - add 'about' to the session/run allowlist to operate it

which is advice nobody can act on. `blocked` is terminal in run_loop, so the run
ended there.

These schemes name no network location: there is nothing to allowlist and
nothing to protect. The tests below hold BOTH halves - they are permitted, and
a real remote host is still refused - because the easy way to fix the first is
to break the second.

Pure: no browser, no network.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.operator import safety as S                      # noqa: E402


def _policy(**kw):
    return S.SafetyPolicy(allowlist=kw.pop("allowlist", []), **kw)


# ── the census 40 case ──────────────────────────────────────────────────────
def test_clicking_on_a_blank_page_is_allowed():
    g = S.evaluate(_policy(), "about:blank", "click", {"ref": "e1"})
    assert g["allowed"] is True


def test_the_blank_page_is_not_reported_as_a_host():
    assert S.is_non_network("about:blank") is True
    assert S.is_non_network("ABOUT:BLANK") is True, "scheme match is case-insensitive"


def test_a_data_url_is_allowed():
    """A run that renders an inline document lands on one of these."""
    assert S.evaluate(_policy(), "data:text/html,<p>x", "click", {})["allowed"] is True


def test_a_browser_error_page_is_allowed():
    """Chrome's own error page - where a failed navigation leaves you, which is
    exactly when the operator most needs to act."""
    assert S.evaluate(_policy(), "chrome-error://chromewebdata/", "click", {})["allowed"] is True


def test_navigating_away_from_blank_to_a_local_page_is_allowed():
    g = S.evaluate(_policy(), "about:blank", "goto",
                   {"url": "http://localhost:8998/x.html"})
    assert g["allowed"] is True


# ── what must STILL be refused ──────────────────────────────────────────────
def test_a_real_external_host_is_still_refused():
    """The property the fix must not trade away."""
    g = S.evaluate(_policy(), "https://example.com/x", "click", {"ref": "e1"})
    assert g["allowed"] is False
    assert "example.com" in g["reason"]


def test_navigating_from_blank_to_an_external_host_is_still_refused():
    """goto is judged on its DESTINATION - sitting on a blank page must not
    become a way to reach anywhere."""
    g = S.evaluate(_policy(), "about:blank", "goto", {"url": "https://evil.com/x"})
    assert g["allowed"] is False


def test_an_allowlisted_external_host_still_works():
    g = S.evaluate(_policy(allowlist=["example.com"]), "https://example.com/x",
                   "click", {})
    assert g["allowed"] is True


def test_a_host_that_merely_starts_with_a_scheme_name_is_not_exempt():
    """"aboutface.com" is a real host and must be judged as one."""
    assert S.is_non_network("https://aboutface.com/x") is False
    assert S.evaluate(_policy(), "https://aboutface.com/x", "click", {})["allowed"] is False


def test_a_scheme_appearing_INSIDE_a_url_does_not_exempt_it():
    """The exemption is a PREFIX test, not a substring one.

    A real host can carry one of these schemes in its query - a redirect
    parameter is the obvious way - and matching anywhere would turn
    "https://evil.com/?next=about:blank" into an unguarded host. An earlier
    version of this test used aboutface.com, which does not contain "about:"
    at all, so it could not tell the two implementations apart.
    """
    sneaky = "https://evil.com/go?next=about:blank"
    assert S.is_non_network(sneaky) is False
    assert S.evaluate(_policy(), sneaky, "click", {})["allowed"] is False
    assert S.is_non_network("https://evil.com/?u=data:text/html,x") is False


def test_read_only_actions_were_never_the_problem():
    """Non-mutating actions were always allowed; this pins that nothing changed
    for them."""
    assert S.evaluate(_policy(), "https://example.com/x", "screenshot", {})["allowed"] is True


def test_dry_run_still_applies_on_a_blank_page():
    """The plan-only gate is orthogonal to the host check and must survive."""
    g = S.evaluate(_policy(dry_run=True), "about:blank", "click", {})
    assert g["allowed"] is True and g["dry_run"] is True
