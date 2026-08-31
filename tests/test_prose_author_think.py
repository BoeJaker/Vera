"""The writer should write, not ruminate.

Measured directly against the model the `writer` role routes to, identical
200-word task, num_predict=4096:

    thinking ON,  no instruction    eval=4096  words=0    21596 chars of <think>
    thinking ON,  "stop at ~200 words, do not add sections"
                                    eval=4096  words=0    17887 chars of <think>
    thinking OFF, no instruction    eval=278   words=227  done="stop"
    thinking OFF, same instruction  eval=303   words=259  done="stop"

Two things that settles. Instructing the model to be brief changed NOTHING -
both instructed runs produced zero words and hit the cap inside their reasoning.
And with reasoning off the model stops on its own at roughly the requested
length, in about a fourteenth of the tokens, without being told to.

This is what made census run 18's prose-only spend 949 seconds in one call and
wall-cap a goal that had completed in run 16.
"""
import ast
import os

SRC = os.path.join(os.path.dirname(__file__), "..", "vera", "dag",
                   "dag_workshop_capabilities.py")


def _fn():
    with open(SRC, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and n.name == "cap_prose_author":
            return n
    raise AssertionError("cap_prose_author not found")


def _generate_calls(fn):
    """Every call in prose.author that passes a caller= label - i.e. its
    generation calls."""
    out = []
    for c in ast.walk(fn):
        if isinstance(c, ast.Call):
            kw = {k.arg: k for k in c.keywords if k.arg}
            if "caller" in kw and "prompt" in kw:
                out.append(kw)
    return out


def test_prose_author_generates_without_the_reasoning_pass():
    """Both of its generation calls, not just the document one."""
    calls = _generate_calls(_fn())
    assert len(calls) >= 2, "expected the document call and the repair call"
    for kw in calls:
        assert "think" in kw, "a prose.author generation call does not set think"


def test_the_default_is_off_but_a_caller_can_ask_for_it():
    """A genuinely long structured document may want the reasoning pass - it
    just has to be asked for, rather than being the silent default."""
    with open(SRC, encoding="utf-8") as fh:
        src = fh.read()
    assert src.count("think=(False if think is None else think)") >= 2
    fn = _fn()
    args = [a.arg for a in fn.args.args] + [a.arg for a in fn.args.kwonlyargs]
    assert "think" in args, "cap_prose_author takes no think option"


def test_the_option_defaults_to_none_not_false():
    """None means 'caller said nothing' and is what the call site turns into
    False - a literal False default would make think=True unreachable through
    a caller that passes the parameter through positionally."""
    fn = _fn()
    names = [a.arg for a in fn.args.args]
    defaults = fn.args.defaults
    offset = len(names) - len(defaults)
    idx = names.index("think") - offset
    node = defaults[idx]
    assert isinstance(node, ast.Constant) and node.value is None


def test_the_measurement_is_recorded_where_the_change_is():
    """The next reader must not have to re-run a 20-minute experiment to learn
    why this is off."""
    with open(SRC, encoding="utf-8") as fh:
        src = fh.read()
    i = src.index("think=(False if think is None else think)")
    window = src[max(0, i - 1600):i]
    assert "eval=4096" in window and "words=0" in window
    assert "eval=278" in window or "words=227" in window


def test_llm_generate_still_accepts_think():
    """The passthrough this depends on."""
    p = os.path.join(os.path.dirname(__file__), "..", "vera", "capabilities",
                     "capabilities.py")
    with open(p, encoding="utf-8") as fh:
        src = fh.read()
    assert "think=think," in src
