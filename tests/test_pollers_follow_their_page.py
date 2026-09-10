"""An element polls only while its page is on screen.

Measured 2026-09-10 on the new Work page: its own poller made 2 requests in
12 s, and the elements of pages that were NOT showing made ~20 more
(task_matrix /evolve/runs?limit=300 every 10 s, test_activity_timeline
limit=500, branch_pipeline, author_map, git_graph, ollama_map), plus a GET
to /ide/git/branches that is a 405 every tick because the capability is
POST. Loop Lab's slowness of the morning was exactly this shape (the
authors endpoint filling the browser's six connections per origin).

Pinned: each polling element checks it is on screen before fetching, the
panel refreshes a page's elements when the page opens, the three section
timers re-arm only on their own page, and the branch element asks the POST
capability the POST way.
"""
import os
import re

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..", "vera")
ELEMENTS = ("task_matrix_element.js", "test_activity_timeline_element.js", "branch_pipeline_element.js",
            "author_map_element.js", "git_graph_element.js", "ollama_routing_map_element.js")


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


@pytest.mark.parametrize("name", ELEMENTS)
def test_the_element_polls_only_on_screen(name):
    src = _read(name)
    assert "_onScreen() { return this.offsetParent !== null; }" in src
    assert re.search(r"setInterval\(\(\) => \{ if \(this\._onScreen\(\)\) this\.refresh\(\); \}", src), name
    assert "if (this._onScreen()) this.refresh();" in src, "the first read too"
    assert not re.search(r"setInterval\(\(\) => this\.refresh\(\)", src), "an unguarded tick remains"


def test_the_branch_element_asks_the_post_capability_the_post_way():
    src = _read("branch_pipeline_element.js")
    assert "this._fetchJson('/ide/git/branches')" not in src, "a GET that is a 405 every tick"
    assert "fetch(this._getBase() + '/ide/git/branches', {\n          method: 'POST'" in src


def test_the_panel_refreshes_a_pages_elements_when_it_opens():
    with open(os.path.join(ROOT, "evolve", "evolve_panel.html"), encoding="utf-8") as fh:
        src = fh.read()
    i = src.index("function nav(sec){")
    nav = src[i:src.index("\n}\n", i)]
    assert "el.tagName.startsWith('VERA-')&&typeof el.refresh==='function'&&el._pollTimer" in nav
    assert "if((ss.length||eq.length)&&_curSec()==='watch'){clearTimeout(window._fleetT)" in src
    assert "if(ps.some(p=>p.live)&&_curSec()==='pipelines'){clearTimeout(window._pipeT)" in src
    assert "if(queue.some(p=>p.live)&&_curSec()==='review'){clearTimeout(window._reviewT)" in src


# ── a sandbox's private Redis actually comes up ──────────────────────────────
def test_the_compose_brings_the_sidecar_up_with_the_app():
    """Found 2026-09-10: the generated compose had the sidecar service, but
    `compose up` named only the app, so the sidecar was never created and the
    app looped on 'Name or service not known' - Redis-less, which the sidecar
    exists to end. The standing mirror has run that way since it was made."""
    with open(os.path.join(ROOT, "evolve", "evolve_capabilities.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert '_depends = "    depends_on:\\n      - %s\\n" % _sbx_redis.sidecar_name(name)' in src
    assert "{_depends}{_redis_sidecar}networks:" in src
    assert '"--force-recreate"] + _services, timeout=300)' in src, "spawn names the sidecar service"
    assert '"--force-recreate", "vera-dev"] + (\n        [_sbx_redis.sidecar_name("vera-dev")] if _sbx_redis is not None else [])' in src, "the primary too"


# ── one event socket per page ────────────────────────────────────────────────
SOCKET_ELEMENTS = ("task_matrix_element.js", "test_activity_timeline_element.js", "branch_pipeline_element.js",
                   "git_graph_element.js", "error_radar_element.js", "ollama_routing_map_element.js")


@pytest.mark.parametrize("name", SOCKET_ELEMENTS)
def test_the_element_rides_the_pages_bus_instead_of_its_own_socket(name):
    src = _read(name)
    assert "function veraSharedEvents(base, fn)" in src
    assert src.count("new WebSocket(") == 1, "only the shared fallback opens a socket"
    assert "this._unsubEvents = veraSharedEvents(this._getBase(), ev =>" in src
    assert "this._unsubEvents && this._unsubEvents(); this._unsubEvents = null;" in src, "disconnect unsubscribes"
    assert "if (typeof window._veraSubscribe === 'function') { const off = window._veraSubscribe(fn);" in src, "the page's own bus first, unconditionally"
    assert "__veraEventsBusOwner" not in src, "found live: a guard on this flag kept every element off the bus (6 sockets, all 403)"


def test_the_panel_declares_its_bus_before_the_elements_load():
    with open(os.path.join(ROOT, "evolve", "evolve_panel.html"), encoding="utf-8") as fh:
        src = fh.read()
    bus = src.index("window._veraSubscribe=fn=>{_busSubs.add(fn)")
    assert bus < src.index('<script src="/ui/elements/error_radar.js">'), "an element subscribes the moment it upgrades"
    assert "function _busDispatch(ev){onEvent(ev);_busSubs.forEach(" in src
    assert "_busDispatch(m.data)" in src and "par._veraSubscribe(_busDispatch)" in src
    assert "window.__veraEventsBusOwner=true;" in src


def test_the_task_matrix_open_cell_opens_the_task_through_time():
    with open(os.path.join(ROOT, "evolve", "evolve_panel.html"), encoding="utf-8") as fh:
        src = fh.read()
    assert "addEventListener('taskmatrix:opencell'" in src and "openTaskHistory(d.task)" in src
