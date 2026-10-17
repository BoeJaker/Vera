"""Terminal WebSocket tickets (pure core): single use, 90 s, bound to the target."""
import os
import sys

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from vera.remote import term_tickets as tt  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_a_ticket_opens_its_target_once():
    book = tt.TicketBook(clock=Clock())
    t = book.mint("ssh", "pve01")
    assert len(t) >= 22                       # 16 random bytes, url-safe
    assert book.consume(t, "ssh", "pve01") == (True, "")
    ok, why = book.consume(t, "ssh", "pve01")
    assert not ok and "already used" in why


def test_tickets_expire_after_the_ttl():
    clk = Clock()
    book = tt.TicketBook(clock=clk)
    t = book.mint("docker", "local", "web")
    clk.t += tt.TTL_S + 0.1
    ok, why = book.consume(t, "docker", "local", "web")
    assert not ok and "expired" in why


def test_a_ticket_for_another_target_is_refused_and_spent():
    book = tt.TicketBook(clock=Clock())
    t = book.mint("docker", "local", "web")
    ok, why = book.consume(t, "docker", "local", "db")
    assert not ok and "another target" in why
    ok, _ = book.consume(t, "docker", "local", "web")
    assert not ok                             # a wrong guess burns the ticket
    t2 = book.mint("ssh", "a")
    assert not book.consume(t2, "docker", "a", "")[0]


def test_no_ticket_and_unknown_ticket_are_refused():
    book = tt.TicketBook(clock=Clock())
    assert book.consume("", "ssh", "a") == (False, "no ticket")
    assert not book.consume("made-up", "ssh", "a")[0]


def test_tickets_are_distinct_and_the_book_is_bounded():
    book = tt.TicketBook(clock=Clock(), max_open=4)
    ts = [book.mint("ssh", f"h{i}") for i in range(10)]
    assert len(set(ts)) == 10
    assert len(book) <= 4
    assert book.consume(ts[-1], "ssh", "h9")[0]
    assert not book.consume(ts[0], "ssh", "h0")[0]     # the oldest was dropped


def test_target_of_path():
    assert tt.target_of_path("/remote/docker/term/ws/local/web%20app?shell=sh") == ("docker", "local", "web app")
    assert tt.target_of_path("/remote/ssh/term/ws/pve01?shell=") == ("ssh", "pve01", "")
    assert tt.target_of_path("/remote/ssh/term/ws/a%2Fb") == ("ssh", "a/b", "")
    for bad in ("", "/ws/mcp", "/remote/docker/term/ws/local", "/remote/docker/term/ws/a/b/c",
                "/remote/ssh/term/ws/", "/remote/ssh/term/ws/a/b", "/proxmox/console/ws/x"):
        assert tt.target_of_path(bad) is None, bad


def test_with_ticket_replaces_an_old_ticket_and_keeps_the_query():
    p = tt.with_ticket("/remote/ssh/term/ws/h?shell=bash&ticket=old", "new")
    assert p.startswith("/remote/ssh/term/ws/h?")
    assert "ticket=new" in p and "old" not in p and "shell=bash" in p
    assert tt.with_ticket("/remote/ssh/term/ws/h", "x") == "/remote/ssh/term/ws/h?ticket=x"
    assert tt.with_ticket("/remote/ssh/term/ws/h?shell=", "x") == "/remote/ssh/term/ws/h?shell=&ticket=x"
