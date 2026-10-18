"""D10-1: the switchboard is honest and read once per request.

- research's worldview gate reads fix.worldview_source_gate (it never did, so switching it off did nothing);
- a chat turn reads the switchboard once however many switches it asks about;
- a seek resolves its switches once, for the seek only, and keeps a run's own switches;
- every switch the code reads is registered, and every registered switch is read somewhere (the reverse).

Runs in the gate container, where Vera.vera resolves to the branch."""
import asyncio
import os
import re
import sys

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import Vera.vera.capability_orchestration as orch  # noqa: E402  (fails loudly, never skips)
from Vera.vera.minds import switchboard_core as SB  # noqa: E402

REG = orch.CAPABILITY_REGISTRY


def run(c):
    return asyncio.run(c)


class Effective:
    """A fake minds.effective that counts its calls."""
    def __init__(self, modes):
        self.modes, self.calls = dict(modes), 0

    async def __call__(self, minds=None, arm=None, trace_id=None):
        self.calls += 1
        return {"modes": dict(self.modes), "source": {}, "ignored": [], "killed": False}


@pytest.fixture
def caps(monkeypatch):
    saved = {k: REG.get(k) for k in ("minds.effective", "worldview.query", "worldview.health")}

    def install(name, fn):
        REG[name] = {"func": fn, "raw": fn}
    yield install
    for k, v in saved.items():
        if v is None:
            REG.pop(k, None)
        else:
            REG[k] = v


class _Citation:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _research_fns(registry):
    """researcher_api has app-level side effects (imports by bare name, makes ./screenshots), so the two functions
    are lifted out of its source with ast and run against a fake registry (the tests/test_gather_memory.py way)."""
    import ast
    import logging
    src = open(os.path.join(ROOT, "vera", "research", "researcher_api.py"), encoding="utf-8").read()
    ns = {"os": os, "sys": sys, "log": logging.getLogger("t"), "Citation": _Citation,
          "CAPABILITY_REGISTRY": registry, "uuid": __import__("uuid"), "time": __import__("time")}
    for n in ast.parse(src).body:
        if isinstance(n, ast.AsyncFunctionDef) and n.name in ("_worldview_gate_mode", "gather_worldview"):
            exec(compile(ast.get_source_segment(src, n), "researcher_api.py", "exec"), ns)
    return ns["gather_worldview"]


def test_research_honours_the_worldview_gate_switch(monkeypatch):
    monkeypatch.delenv("VERA_RESEARCH_WORLDVIEW_UNGATED", raising=False)

    async def query(text, top_k=15, trace_id=None):
        return {"results": [{"text": "a worldview neighbour", "concept_label": "c", "score": 0.9}]}

    async def health(trace_id=None):
        return {"status": "overfit", "reasons": ["test"]}
    reg = {"worldview.query": {"func": query, "raw": query}, "worldview.health": {"func": health, "raw": health}}
    gather = _research_fns(reg)
    reg["minds.effective"] = {"func": Effective({"fix.worldview_source_gate": "on"})}
    assert run(gather("q")) == []                               # 'on': an unhealthy worldview is not cited
    reg["minds.effective"] = {"func": Effective({"fix.worldview_source_gate": "off"})}
    assert len(run(gather("q"))) == 1                           # 'off': the old ungated behaviour, as documented
    tok = SB.RUN_MINDS.set({"modes": {"fix.worldview_source_gate": "on"}})
    try:
        assert run(gather("q")) == []                           # a run's own switches win over the global layer
    finally:
        SB.RUN_MINDS.reset(tok)
    monkeypatch.setenv("VERA_RESEARCH_WORLDVIEW_UNGATED", "1")
    tok = SB.RUN_MINDS.set({"modes": {"fix.worldview_source_gate": "on"}})
    try:
        assert len(run(gather("q"))) == 1                       # the env override still forces ungated
    finally:
        SB.RUN_MINDS.reset(tok)


def test_a_chat_turn_reads_the_switchboard_once(caps):
    import Vera.vera.agents.agents as ag
    eff = Effective({"context.needs_web": "rules", "context.needs_memory": "off", "sense.chat_pre": "on"})
    caps("minds.effective", eff)
    body = {"minds": None}

    async def turn():
        return [await ag._switch_mode("context.needs_web", None, cache=body),
                await ag._switch_mode("context.needs_memory", None, cache=body),
                await ag._switch_mode("sense.chat_pre", None, cache=body),
                await ag._switch_mode("tools.shortlist.chat", None, default="legacy", cache=body)]
    assert run(turn()) == ["rules", "off", "on", "legacy"]
    assert eff.calls == 1


def test_a_seek_resolves_once_for_itself_only(caps):
    import Vera.vera.fabric.memory_retrieval as mr
    eff = Effective({"memory.seek_cache": "on"})
    caps("minds.effective", eff)

    async def seek():
        async with mr._seek_minds_scope():
            inside = [await mr._switch("memory.seek_cache"), await mr._switch("memory.seek_cache")]
        return inside, SB.RUN_MINDS.get()
    inside, after = run(seek())
    assert inside == ["on", "on"] and eff.calls == 1 and after is None   # one read; nothing leaks out

    tok = SB.RUN_MINDS.set({"modes": {"memory.seek_cache": "off"}})
    try:
        async def in_a_run():
            async with mr._seek_minds_scope():
                return await mr._switch("memory.seek_cache")
        assert run(in_a_run()) == "off" and eff.calls == 1            # a run's switches are kept, no new read
    finally:
        SB.RUN_MINDS.reset(tok)


READ = re.compile(r"""(?:mode_of|_switch_mode|_switch)\(\s*["']([a-z0-9_.]+)["']""")


def _sources():
    for base, _d, files in os.walk(os.path.join(ROOT, "vera")):
        for n in files:
            if n.endswith((".py", ".js", ".html")) and n != "switchboard_core.py":
                try:
                    yield os.path.join(base, n), open(os.path.join(base, n), encoding="utf-8", errors="ignore").read()
                except OSError:
                    continue


def test_every_switch_read_by_any_reader_shape_is_registered():
    missing = {fid for _p, src in _sources() for fid in READ.findall(src) if fid not in SB.REGISTRY}
    assert not missing, f"switches read but not registered: {sorted(missing)}"


def test_every_registered_switch_is_read_somewhere():
    """The reverse (D10-1): a switch nothing reads is a control that does nothing (fix.worldview_source_gate was)."""
    blob = "\n".join(src for _p, src in _sources())
    unread = [fid for fid in SB.REGISTRY if f'"{fid}"' not in blob and f"'{fid}'" not in blob
              and not (fid.startswith("decide.student.") and "decide.student." in blob)]
    assert not unread, f"registered switches nothing reads: {unread}"
