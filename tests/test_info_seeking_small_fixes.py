"""Info-seeking plan SK-0.5 / SK-2.2 (shared-planning/intelligence/PLAN-INFO-SEEKING.md): the dream probe kit's
search probes get their search text, research.run survives a guessed mode, a failed research job says where it
broke, and session notes no longer tell the model to trust world facts in them."""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "vera"


def _src(*p):
    return ROOT.joinpath(*p).read_text(encoding="utf-8")


def test_probe_kit_names_the_real_search_arguments():
    s = _src("dream", "dream_capabilities.py")
    assert '"research.db.search":     {"desc": "search indexed research/news findings (fast)", "args": "{q:str,limit:int}"}' in s
    assert '"fabric.discover.query":  {"desc": "query discovered/crawled external sources", "args": "{question:str}"}' in s
    tree = ast.parse(s)
    m = next(n for n in tree.body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "_PROBE_TEXT_ARG")
    assert ast.literal_eval(m.value) == {"research.db.search": "q", "fabric.discover.query": "question"}
    body = s[s.index("async def _narrator_run_probe"):]
    body = body[:body.index("\n\n\n")]
    assert 'args[_text_arg] = args.pop("query")' in body          # the old hint (and the built-in probes) still map


def test_probe_query_mapping_behaviour():
    # the mapping lines, run on their own
    _PROBE_TEXT_ARG = {"research.db.search": "q", "fabric.discover.query": "question"}

    def fix(name, args):
        args = dict(args)
        _text_arg = _PROBE_TEXT_ARG.get(name)
        if _text_arg and "query" in args and not args.get(_text_arg):
            args[_text_arg] = args.pop("query")
        return args
    assert fix("research.db.search", {"query": "x", "limit": 5}) == {"q": "x", "limit": 5}
    assert fix("fabric.discover.query", {"query": "x"}) == {"question": "x"}
    assert fix("research.db.search", {"q": "kept", "query": "x"}) == {"q": "kept", "query": "x"}
    assert fix("memory.recall", {"query": "x"}) == {"query": "x"}


def test_research_run_falls_back_on_an_unknown_mode():
    s = _src("research", "researcher_api.py")
    body = s[s.index("async def cap_research_run"):]
    body = body[:body.index("# ── Convenience aliases")]
    assert "AgentMode(mode)" not in body and "OutputMode(output_mode)" not in body
    assert "_mode = AgentMode(\"single\")" in body and "_omode = OutputMode(\"report\")" in body
    assert 'out["note"] = "; ".join(_note)' in body


def test_a_failed_job_records_where_it_broke():
    s = _src("research", "researcher_api.py")
    assert "job.error = str(e) + _error_where(e)" in s
    ns = {"os": __import__("os")}
    fn = s[s.index("def _error_where"):s.index("async def run_job")]
    exec(fn, ns)
    try:
        ", ".join(["a", "b", ["c"]])
    except TypeError as e:
        where = ns["_error_where"](e)
    assert re.match(r" \(at test_info_seeking_small_fixes\.py:\d+ in test_a_failed_job_records_where_it_broke\)$", where)
    assert ns["_error_where"](ValueError("no traceback")) == ""


def test_session_notes_are_dated_and_not_trusted_for_world_facts():
    s = _src("fabric", "session_notes.py")
    assert "trust and follow it" not in s
    assert "## Session memory (agent-maintained notes: follow the user's preferences" in s
    assert "facts about the outside world in them are as of the date shown - check anything current" in s
    assert "', updated ' + upd if upd else ''" in s
