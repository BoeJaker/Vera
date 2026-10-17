"""FM-3.3 'on' (owner, 17 Oct): a chat agent reaches every SAFE capability through a shortlist fitted to the message;
anything unsure stays out, and the page falls back to its own list on any failure."""
import asyncio
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import chat_caps_core as CC  # noqa: E402
from vera.minds import switchboard_core as SB  # noqa: E402

pytestmark = pytest.mark.critical


def _e(effects=None, approval="not_required", expose=True):
    c = {"effects": effects, "approval": {"status": approval}} if effects is not None else {}
    return {"contract": c, "mcp_expose": expose, "description": "d"}


@pytest.mark.parametrize("name,entry,ok", [
    ("web.search", _e(["read", "network"]), True),
    ("memory.seek", _e(["read", "model"]), True),
    ("fs.write", _e(["write", "filesystem"]), False),
    ("markets.quote", _e(["read"], approval="required"), False),
    ("exec.bash.run", _e(["read"]), False),                       # a shell, whatever it declares
    ("secrets.list", _e(), False),
    ("weather.forecast", _e(), True),                             # no contract: a lookup-like name
    ("intel.topic.delete", _e(), False),                          # no contract: a change
    ("canvas.add.note", _e(), False),
    ("widgets.frobnicate", _e(), False),                          # unsure: left out
    ("web.fetch", _e(["read", "network"], expose=False), False),  # not exposed to agents
])
def test_only_safe_capabilities_are_offered(name, entry, ok):
    assert CC.safe(name, entry)[0] is ok


def test_the_catalogue_and_the_fusion():
    reg = {"web.search": _e(["read", "network"]), "exec.bash.run": _e(["execute"]), "news.latest": _e(),
           "fs.delete": _e()}
    cat = CC.catalogue(reg)
    assert set(cat) == {"web.search", "news.latest"} and cat["web.search"]["why"].startswith("contract")
    f = CC.fuse({"past_steps": ["exec.bash.run", "web.search"], "keywords": ["news.latest", "web.search"]}, cat, 5)
    assert [x["name"] for x in f] == ["web.search", "news.latest"] and f[0]["by"] == "past_steps+keywords"
    assert "exec.bash.run" not in {x["name"] for x in f}
    assert CC.card("web.search", {"description": "Search the web."}) == "web search: Search the web."


def test_the_switch_has_an_on_mode_and_stays_legacy_by_default():
    f = SB.REGISTRY["tools.shortlist.chat"]
    assert f.modes == ("legacy", "shadow", "on") and f.default == "legacy"


def test_the_server_answers_only_when_on_and_only_with_safe_caps(monkeypatch):
    from vera.minds import tool_shortlist as T
    reg = {"web.search": {**_e(["read", "network"]), "description": "Search the web for pages."},
           "news.latest": {**_e(), "description": "The latest news headlines."},
           "exec.bash.run": {**_e(["execute"]), "description": "Run a shell command to search files."}}

    async def effective(minds=None, **kw):
        return {"modes": {"tools.shortlist.chat": state["mode"]}}

    async def knn(text="", k=10, exclude_run="", trace_id=None):
        return {"tools": [{"name": "exec.bash.run"}, {"name": "web.search"}]}
    state = {"mode": "legacy"}
    monkeypatch.setattr(T, "CAPABILITY_REGISTRY", dict(reg, **{"minds.effective": {"func": effective}}))
    monkeypatch.setattr(T, "_SAFE", {"at": 0.0, "cat": {}})
    monkeypatch.setattr(T, "cap_caps_shortlist", knn)
    assert asyncio.run(T.cap_caps_chat_shortlist(text="search the latest news")) == {"mode": "legacy", "tools": []}
    state["mode"] = "on"
    out = asyncio.run(T.cap_caps_chat_shortlist(text="search the latest news"))
    names = [t["name"] for t in out["tools"]]
    assert out["mode"] == "on" and "web.search" in names and "exec.bash.run" not in names and out["safe_total"] == 2


def test_the_page_asks_before_the_turn_and_keeps_its_own_list_on_failure():
    src = open(os.path.join(ROOT, "vera", "chat", "chat_panel.html"), encoding="utf-8").read()
    assert "try{ await _fetchChatShortlist(msg); }catch(_){}\n    const capHint=buildCapHint(a,msg);" in src
    assert "setTimeout(()=>ctl.abort(),900)" in src and "name:'caps.chat_shortlist'" in src
    body = src[src.index("function buildCapHint(a,query){"):]
    body = body[:body.index("relevantNames.forEach")]
    assert "if(!allowedCaps.length && !serverSafe.length) return '';" in body
    assert "[...new Set([...serverSafe,...relevantNames.slice(0,10)])].slice(0,34)" in body
    assert re.search(r"c\.mode==='on'", src)
