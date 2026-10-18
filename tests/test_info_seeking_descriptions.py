"""The information caps say what they are for, and only one of them claims "the latest / news" (info-seeking plan
SK-0.4 / SK-0.6, shared-planning/intelligence/PLAN-INFO-SEEKING.md). Before: research.run and research.report both
claimed "latest news on", research.quick_search sounded fast but is a report job of minutes, and the chat's default
agent listed it as a web tool - so a "what's new on X" could start a 10-30 minute job."""
import ast
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "vera"


def _src(*p):
    return (ROOT.joinpath(*p)).read_text(encoding="utf-8")


def _cap_description(src: str, name: str) -> str:
    """The description= string literal of the @capability that registers `name` (no import: these modules are heavy)."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) \
                and node.args[0].value == name:
            for kw in node.keywords:
                if kw.arg == "description":
                    return ast.literal_eval(kw.value)
    raise AssertionError(f"{name} not found")


def test_only_the_fast_cap_claims_latest_news():
    web = _cap_description(_src("web", "web_capabilities.py"), "web.research")
    assert re.search(r"latest news|updates on", web) and "seconds" in web
    research = _src("research", "researcher_api.py")
    for name in ("research.run", "research.report", "research.quick_search"):
        d = _cap_description(research, name)
        assert "web.research" in d, name                         # each points to the fast cap
        assert not re.search(r"'(what is the )?latest( news)? on'", d), name
    assert "BACKGROUND" in _cap_description(research, "research.run")
    assert "NOT a quick search" in _cap_description(research, "research.quick_search")


def test_web_research_is_tagged_for_the_chat_picker():
    tree = ast.parse(_src("web", "web_capabilities.py"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args and getattr(node.args[0], "value", None) == "web.research":
            tags = next(ast.literal_eval(k.value) for k in node.keywords if k.arg == "tags")
            assert {"latest", "news", "updates", "current"} <= set(tags)
            return
    raise AssertionError("web.research not found")


def _agents_module_values():
    tree = ast.parse(_src("agents", "agents.py"))
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and \
                node.targets[0].id in ("AIDE_PROMPT", "_AIDE_PRIOR_PROMPTS", "_AIDE_RETIRED_CAPS"):
            out[node.targets[0].id] = ast.literal_eval(node.value)
    return out, tree


def test_the_aide_looks_things_up_and_keeps_research_run_for_reports():
    v, tree = _agents_module_values()
    p = v["AIDE_PROMPT"]
    assert "info.seek FIRST" in p and "BACKGROUND report job" in p and "intel.lookup" in p
    assert "dated records" in p and "Never fill a gap with a plausible guess" in p
    assert "quick_search" not in p
    # the aide's default toolkit no longer lists the slow "quick" search
    aide = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "AgentRecord"
                and any(k.arg == "name" and getattr(k.value, "value", "") == "aide" for k in n.keywords))
    caps = ast.literal_eval(next(k.value for k in aide.keywords if k.arg == "domain_caps"))
    assert "research.quick_search" not in caps and "web.research" in caps and "research.run" in caps
    assert {"info.seek", "intel.lookup", "intel.topic.save"} <= set(caps)


def test_an_untouched_aide_is_upgraded_and_a_customised_one_is_not():
    v, _ = _agents_module_values()
    cur = hashlib.sha1(v["AIDE_PROMPT"].encode()).hexdigest()
    assert cur not in v["_AIDE_PRIOR_PROMPTS"]                   # the current default is not an "old" one
    assert "7a9ac80b6a8c4023d1cca601ae12610412a31fb3" in v["_AIDE_PRIOR_PROMPTS"]   # prod's stored prompt, 10 Oct
    assert "68b6c2325f754d4be4b354004fbb02956eab2088" in v["_AIDE_PRIOR_PROMPTS"]   # ...and after the first release
    assert v["_AIDE_RETIRED_CAPS"] == ("research.quick_search",)
    seed = _src("agents", "agents.py")
    seed = seed[seed.index("async def _seed_defaults"):]
    seed = seed[:seed.index("\n# ───")]
    assert 'hashlib.sha1((existing.system_prompt or "").encode()).hexdigest() in _AIDE_PRIOR_PROMPTS' in seed
    assert "not in _AIDE_RETIRED_CAPS" in seed


def test_pre_answer_snippets_send_the_model_to_info_seek():
    # owner 2026-10-11: info.seek is never triggered before the answer - the model calls it; the quick snippet
    # pre-fetch must say it is not the evidence
    src = _src("agents", "agents.py")
    body = src[src.index("def _format_web_context"):src.index("async def _safe_gen")]
    assert "Ground your answer in these results" not in body
    assert "SNIPPETS only" in body and "call info.seek before answering" in body
    assert "info.seek" not in src[src.index("async def _run_web_search"):src.index("_web_task = asyncio.create_task")]


def test_chat_and_the_loop_use_the_same_memory_pair():
    # info-seeking plan SK-2.6: the loop's canonical memory.seek / memory.read; the aide had only recall/search
    v, tree = _agents_module_values()
    assert "memory.seek (one search over every store), then memory.read" in v["AIDE_PROMPT"]
    aide = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "AgentRecord"
                and any(k.arg == "name" and getattr(k.value, "value", "") == "aide" for k in n.keywords))
    caps = ast.literal_eval(next(k.value for k in aide.keywords if k.arg == "domain_caps"))
    assert {"memory.seek", "memory.read", "memory.recall"} <= set(caps)
    assert "b99329fc86df0e71ed4d6f4fe7da18a21d9c89a7" in v["_AIDE_PRIOR_PROMPTS"]
