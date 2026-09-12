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
    # offsetParent alone missed a closed <details> (Chromium keeps its contents
    # laid out, content-visibility: hidden); checkVisibility() sees it (2026-09-10).
    assert "_onScreen() { return this.offsetParent !== null && (typeof this.checkVisibility !== 'function' || this.checkVisibility()); }" in src
    assert re.search(r"setInterval\(\(\) => \{ if \(this\._onScreen\(\)\) this\.refresh\(\); \}", src), name
    assert "if (this._onScreen()) this.refresh();" in src, "the first read too"
    assert not re.search(r"setInterval\(\(\) => this\.refresh\(\)", src), "an unguarded tick remains"


def test_the_branch_element_asks_the_post_capability_the_post_way():
    src = _read("branch_pipeline_element.js")
    assert "this._fetchJson('/ide/git/branches')" not in src, "a GET that is a 405 every tick"
    assert "fetch(this._getBase() + '/ide/git/branches', {\n          method: 'POST'" in src


def _fn(src, name):
    start = src.index("function " + name + "(")
    return src[start:src.index("\n}\n", start)]


def test_the_run_once_theatres_clocks_tick_only_while_its_fold_is_on_screen():
    """setInterval(refreshActiveRun,4000) polled /evolve/run/status on every
    page for as long as a run id was remembered, and the 8 s adopt timer
    re-read it too (2026-09-10). Both live inside the Run-once fold now."""
    with open(os.path.join(ROOT, "evolve", "evolve_panel.html"), encoding="utf-8") as fh:
        src = fh.read()
    assert "setInterval(refreshActiveRun,4000)" not in src
    assert "function _theatreOnScreen(){const p=$('mc-live'),r=$('mc-live-run');return _curSec()==='mission'&&!!p&&p.style.display!=='none'&&!!r&&r.style.display!=='none'}" in src
    assert "setInterval(()=>{if(_theatreOnScreen())refreshActiveRun()},4000);" in src
    assert "_testAdoptTimer=setInterval(()=>{if(!_implTimer&&_theatreOnScreen())restoreTestRun()},8000);" in src
    assert "_mcRefreshT=setTimeout(missionPoll,4000)" in src, "a burst of bus events is one refresh"
    assert "if(typeof el.checkVisibility==='function'&&!el.checkVisibility()){s.timer=setTimeout(()=>cenAloPoll(id),3000);return}" in _fn(src, "cenAloPoll"), \
        "the live dash's follow reads nothing while its page is off screen"


def test_the_panel_refreshes_a_pages_elements_when_it_opens():
    with open(os.path.join(ROOT, "evolve", "evolve_panel.html"), encoding="utf-8") as fh:
        src = fh.read()
    i = src.index("function nav(sec){")
    nav = src[i:src.index("\n}\n", i)]
    assert "el.tagName.startsWith('VERA-')&&typeof el.refresh==='function'&&el._pollTimer" in nav
    # The Watch page's fleet is the Agents table now; its own poller is gone.
    assert "_fleetT" not in src
    # CI/CD and Review are the Ship page since slice 5: one poller for the
    # table, the full pipeline list's own only while its fold is open.
    assert "if(r.any_live&&$('ship-follow')&&$('ship-follow').checked&&_curSec()==='ship')window._shipT=setTimeout(loadShip,6000)" in src
    assert "if(ps.some(p=>p.live)&&_curSec()==='ship'&&$('ship-pipes')&&$('ship-pipes').open){clearTimeout(window._pipeT)" in src
    assert "_reviewT" not in src


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
