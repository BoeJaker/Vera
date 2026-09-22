# -*- coding: utf-8 -*-
"""flow (EXPLODE-R2.md R4): what ONE function DOES.

The dependency layout answers "what calls what" across a file; it cannot answer "what does this do". Flow reads
one function in SOURCE ORDER: every call it makes, in the order it makes them, each in the branch that encloses
it, returns and raises marked. These checks pin the three things the plan asks for -- the call order IS the source
order, every branch band holds exactly the calls made inside it, and an unparsed thing is still a card, not a gap.
"""
import asyncio

import pytest

from vera.research import code_explode_core as C
from vera.research import explode_capabilities as X

SRC = '''"""m"""
import os


def outer(rows, flag):
    n = len(rows)
    if not rows:
        log("empty")
        return 0
    for r in rows:
        try:
            parse(r)
        except ValueError:
            recover(r)
            continue
        else:
            accept(r)
        finally:
            close(r)
    if flag:
        n += 1
    total = sum(weigh(r) for r in rows)
    return max(total, n)


def inner():
    return outer([], False)
'''


def flow(focus="outer", src=SRC):
    return C.flow_of("m.py", src, focus)


def steps(d):
    return [(c["step"], c["kind"], c["title"], c["group"], c["span"]["line"]) for c in d["cards"]]


def test_the_calls_are_the_calls_the_source_makes_in_the_order_it_makes_them():
    d = flow()
    calls = [(c["title"], c["span"]["line"]) for c in d["cards"] if c["kind"] == "call"]
    assert calls == [("len", 6), ("log", 8), ("parse", 12), ("recover", 14), ("accept", 17),
                     ("close", 19), ("weigh", 22), ("sum", 22), ("max", 23)], calls
    # the lines never go backwards, and the step numbers are the reading order
    lines = [c["span"]["line"] for c in d["cards"][1:]]
    assert lines == sorted(lines), lines
    assert [c["step"] for c in d["cards"]] == list(range(len(d["cards"])))


def test_a_nested_call_reads_before_the_call_it_is_an_argument_of():
    """`sum(weigh(r) ...)`: both start at the same column, so only the END tells them apart -- and weigh() is
    both what a reader meets first and what runs first."""
    d = flow()
    order = [c["title"] for c in d["cards"] if c["kind"] == "call" and c["span"]["line"] == 22]
    assert order == ["weigh", "sum"]


def test_every_branch_band_holds_exactly_the_calls_made_inside_it():
    d = flow()
    by_label = {g["id"]: g["label"] for g in d["groups"]}
    where = {}
    for c in d["cards"]:
        if c["kind"] in ("call", "return", "raise"):
            where.setdefault(by_label[c["group"]], []).append(c["title"])
    assert where["if not rows"] == ["log", "return"]
    assert where["except ValueError"] == ["recover"]
    assert where["else"] == ["accept"]
    assert where["finally"] == ["close"]
    assert where["outer"] == ["len", "weigh", "sum", "max", "return"]      # the function's own body
    # `parse` is in the try, which is in the for: a band holds what is written IN it, not what its children hold
    assert "for r in rows" not in where and where["try"] == ["parse"]
    # A branch is a band inside the band it is WRITTEN in, and the paths of one statement are SIBLINGS: an
    # `except` does not happen inside the try's body, it happens instead of the rest of it, so the four bands of
    # a try/except/else/finally stand side by side in the band that encloses the statement.
    g = {x["id"]: x for x in d["groups"]}
    forg = next(x for x in d["groups"] if x["label"] == "for r in rows")
    paths = [x for x in d["groups"] if x["label"] in ("try", "except ValueError", "else", "finally")]
    assert len(paths) == 4 and {g[x["parent"]]["label"] for x in paths} == {"for r in rows"}
    assert g[forg["parent"]]["label"] == "outer"       # and the for is written in the function's own body


def test_a_branch_that_makes_no_call_is_still_drawn():
    """`if flag: n += 1` makes no call at all. The renderer does not draw an empty plate, so without a card of its
    own that branch would vanish from a diagram whose whole job is to show the branches."""
    d = flow()
    band = next(g for g in d["groups"] if g["label"] == "if flag")
    own = [c for c in d["cards"] if c["group"] == band["id"]]
    assert len(own) == 1 and own[0]["kind"] == "step" and own[0]["title"] == "n += 1"
    assert "no call" in own[0]["subtitle"]


def test_returns_are_marked_and_the_function_itself_is_the_first_card():
    d = flow()
    assert d["cards"][0]["kind"] == "function" and d["cards"][0]["step"] == 0
    rets = [c for c in d["cards"] if c["kind"] == "return"]
    assert len(rets) == 2 and all("return" in c["badges"] for c in rets)
    # if / for / try / except / else / finally / if flag
    assert d["counts"]["calls"] == 9 and d["counts"]["branches"] == 7


def test_every_step_follows_the_one_before_it_and_a_branch_joins_back():
    """The runs ARE the order. After a try/except/else, what comes next follows every path that can reach it."""
    d = flow()
    by_id = {c["id"]: c for c in d["cards"]}
    for e in d["edges"]:
        assert by_id[e["from"]]["step"] < by_id[e["to"]]["step"]
        assert e["resolution"] == "exact" and e["layer"] == "code.flow"
    # `close` (finally) is reached from the paths through the try
    close = next(c for c in d["cards"] if c["title"] == "close")
    assert len([e for e in d["edges"] if e["to"] == close["id"]]) >= 1
    weigh = next(c for c in d["cards"] if c["title"] == "weigh")
    assert len([e for e in d["edges"] if e["to"] == weigh["id"]]) >= 1


def test_the_focus_can_be_a_bare_name_a_qualified_name_or_a_card_id():
    src = "class A:\n    def go(self):\n        return one()\n\n\ndef go():\n    return two()\n"
    assert C.flow_of("m.py", src, "A.go")["cards"][1]["title"] == "one"
    assert C.flow_of("m.py", src, C._sid("m.py", "A.go"))["focus"] == "A.go"
    assert C.flow_of("m.py", src, "go")["focus"] in ("A.go", "go")       # first match wins, and it says which


def test_a_function_that_is_not_there_lists_the_ones_that_are():
    d = flow(focus="nope")
    assert "error" in d and "no function 'nope'" in d["error"]
    assert "outer" in d["error"] and "inner" in d["error"]


def test_flow_is_python_only_and_says_so_rather_than_guessing():
    d = C.flow_of("a.js", "function f(){ g(); }\n", "f")
    assert "error" in d and "python only" in d["error"] and "javascript" in d["error"]
    assert "tree-sitter" in d["error"]


def test_the_capability_flows_a_repo_file_and_the_contract_is_the_flow_shape():
    d = asyncio.get_event_loop().run_until_complete(
        X.explode_code(path="vera/research/assess_core.py", flow="readability"))
    assert d["ok"] and d["layout"]["mode"] == "flow" and d["focus"] == "readability"
    assert d["counts"]["calls"] > 5 and d["counts"]["branches"] >= 1
    assert all(c.get("step") is not None for c in d["cards"])
    assert d["layers"][0]["id"] == "code.flow"
    bad = asyncio.get_event_loop().run_until_complete(
        X.explode_code(path="vera/research/assess_core.py", flow="not_a_function"))
    assert "error" in bad and "no function" in bad["error"]
