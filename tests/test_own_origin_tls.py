"""Trust the certificate we issued to ourselves, and nobody else's.

Census run 19, build-browser-verified: operator.run could not get a browser, so
the executor sensibly tried to fetch the page instead - http.get once and
web.fetch three times - and all four died on

    [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed

against Vera's OWN sandbox-preview URL. It then spent 532 seconds trying to
apt-get install xvfb and firefox-esr to get a browser of its own.

The danger in fixing this is obvious - a blanket verify=False would silently
disable TLS verification for the open internet. So every test below that proves
the relaxation works is paired with one proving it does NOT apply anywhere else.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from vera.web.own_origin import is_own_origin, verify_for   # noqa: E402

HOSTS = ["llm.int"]
PORT = 8999


def own(url):
    return is_own_origin(url, own_hosts=HOSTS, own_port=PORT)


# --- ours -------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://localhost:8999/remote/sandbox/preview/chat-1/form.html",
    "https://127.0.0.1:8999/ui/panel/window?id=markets",
    "https://llm.int:8999/health",
    "http://localhost:8999/anything",
])
def test_our_own_origin_is_recognised(url):
    assert own(url) is True
    assert verify_for(url, own_hosts=HOSTS, own_port=PORT) is False


# --- everyone else ----------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://example.com/",
    "https://github.com/VrushankPatel/godseye",
    "https://api.openai.com/v1/models",
    "https://en.wikipedia.org/wiki/Race_condition",
])
def test_the_open_internet_keeps_full_verification(url):
    assert own(url) is False
    assert verify_for(url, own_hosts=HOSTS, own_port=PORT) is True


def test_a_different_port_on_our_own_host_is_not_us():
    """Another service on this box is still a stranger."""
    assert own("https://localhost:9999/x") is False
    assert own("https://llm.int:443/x") is False


def test_a_hostname_that_merely_contains_ours_is_not_us():
    assert own("https://llm.int.evil.com:8999/x") is False
    assert own("https://notlocalhost:8999/x") is False


def test_localhost_in_a_query_string_does_not_count():
    assert own("https://evil.com/?next=https://localhost:8999/") is False


def test_a_url_with_credentials_pointing_elsewhere_is_not_us():
    assert own("https://localhost:8999@evil.com/x") is False


# --- shape ------------------------------------------------------------------

def test_an_https_url_with_no_port_only_counts_if_we_serve_443():
    assert is_own_origin("https://localhost/x", own_hosts=HOSTS, own_port=8999) is False
    assert is_own_origin("https://localhost/x", own_hosts=HOSTS, own_port=443) is True


def test_unknown_hosts_default_to_loopback_only():
    """own_identity() returns no hosts when config cannot be read - the
    conservative answer, not a wide one."""
    assert is_own_origin("https://llm.int:8999/x", own_hosts=[], own_port=8999) is False
    assert is_own_origin("https://localhost:8999/x", own_hosts=[], own_port=8999) is True


def test_junk_is_never_ours():
    for junk in ("", None, "not a url", "ftp://localhost:8999/x", "file:///etc/passwd"):
        assert is_own_origin(junk, own_hosts=HOSTS, own_port=PORT) is False


def test_no_port_constraint_still_requires_a_known_host():
    assert is_own_origin("https://example.com/x", own_hosts=HOSTS, own_port=None) is False
    assert is_own_origin("https://localhost/x", own_hosts=HOSTS, own_port=None) is True


# --- the callsites ----------------------------------------------------------

def test_both_fetch_paths_consult_it():
    for rel in (("vera", "web", "web_client.py"),
                ("vera", "capabilities", "capabilities.py")):
        p = os.path.join(os.path.dirname(__file__), "..", *rel)
        with open(p, encoding="utf-8") as fh:
            src = fh.read()
        assert "own_origin" in src and "verify=_verify" in src, rel[-1]


def test_the_default_is_still_verified():
    """new_session() with no url must not relax anything."""
    p = os.path.join(os.path.dirname(__file__), "..", "vera", "web", "web_client.py")
    with open(p, encoding="utf-8") as fh:
        src = fh.read()
    i = src.index("_verify = True")
    assert "if url:" in src[i:i + 120], "the relaxation must be gated on having a url"
