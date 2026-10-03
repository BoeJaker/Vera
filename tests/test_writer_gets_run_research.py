"""The writer gets the RUN's research, not only its own step's (probe 3 Oct, 1e84cf1d:
research in steps 1-2, prose.author in step 3, nothing handed over, no URLs cited)."""

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("rsc2", ROOT / "vera" / "dag" / "research_sources_core.py")
C = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(C)
SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")


def _web(i):
    return {"tool": "web.fetch", "ok": True, "args": {"url": "https://ex.com/%d" % i}, "preview": "page %d" % i}


def test_remember_recall_bounded_and_only_sources():
    C._RUN.clear()
    C.remember("s1", _web(1))
    C.remember("s1", {"tool": "exec.bash.run", "ok": True, "preview": "x"})
    C.remember("s1", {"tool": "web.fetch", "ok": False, "preview": "ERROR"})
    C.remember("", _web(2))
    assert [e["args"]["url"] for e in C.recall("s1")] == ["https://ex.com/1"]
    for i in range(30):
        C.remember("s2", _web(i))
    assert len(C.recall("s2")) == C.MAX_PER_RUN and C.recall("s2")[-1]["args"]["url"].endswith("/29")
    for i in range(C.MAX_RUNS + 5):
        C.remember("run%d" % i, _web(i))
    assert len(C._RUN) == C.MAX_RUNS and C.recall("s1") == []
    assert C.recall("nope") == []


def test_earlier_step_research_reaches_the_block_once():
    C._RUN.clear()
    C.remember("s", _web(1))
    step = [_web(1), {"tool": "prose.author", "ok": True, "preview": "x"}]   # also in this step
    b = C.sources_block(C.recall("s") + step)
    assert b.count("https://ex.com/1") == 1


def test_wiring():
    assert SRC.count("_research_sources_core.remember(sid, history[-1])") == 2
    assert "_research_sources_core.recall(sid) + list(history)" in SRC
