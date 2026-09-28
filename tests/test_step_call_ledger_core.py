"""The executor must see every call its step already made (step_call_ledger_core).

Census runs 70-78: steps of 10-18 cycles re-issued calls that had already failed,
because each cycle's prompt shows only the last four results. Pure module - the
lowercase import with the repo root on the path resolves to THIS checkout.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from vera.dag import step_call_ledger_core as L  # noqa: E402


def _h(n, ok=True, tool="exec.bash.run", preview=None, args=None):
    return {"tool": tool, "ok": ok, "args": args or {"command": f"cmd-{n}"},
            "preview": preview if preview is not None else f"out-{n}\nsecond line"}


def _summ(args):
    return ",".join(f"{k}={v}" for k, v in (args or {}).items())


def test_a_short_step_gets_nothing_so_its_prompt_is_unchanged():
    for n in range(0, 5):
        assert L.earlier_calls_block([_h(i) for i in range(n)], summarise=_summ) == ""


def test_the_calls_older_than_the_last_four_are_listed_in_order():
    hist = [_h(i) for i in range(1, 8)]                       # 7 calls, 3 older than shown
    out = L.earlier_calls_block(hist, summarise=_summ)
    assert out.startswith(L.HEADER)
    assert "call 1: exec.bash.run(command=cmd-1) -> ok: out-1" in out
    assert "call 3: exec.bash.run(command=cmd-3)" in out
    assert "cmd-4" not in out                                  # 4..7 are shown in full elsewhere
    assert out.index("call 1:") < out.index("call 2:") < out.index("call 3:")
    assert "second line" not in out                            # one line per call


def test_a_failed_call_says_failed_with_the_first_line_of_its_error():
    hist = [_h(1, ok=False, preview="\n  FAILED tests/test_stats.py::test_mode\ntrace...")] \
        + [_h(i) for i in range(2, 6)]
    out = L.earlier_calls_block(hist, summarise=_summ)
    assert "call 1: exec.bash.run(command=cmd-1) -> FAILED: FAILED tests/test_stats.py::test_mode" in out


def test_a_long_step_lists_the_most_recent_and_counts_the_rest():
    hist = [_h(i) for i in range(1, 31)]                       # 26 older than shown
    out = L.earlier_calls_block(hist, summarise=_summ, max_lines=16)
    assert "(10 earlier call(s) not listed)" in out
    assert "call 11:" in out and "call 26:" in out
    assert "call 10:" not in out


def test_the_block_never_exceeds_its_ceiling_and_keeps_the_newest():
    big = "x" * 400
    hist = [_h(i, preview=big, args={"command": big}) for i in range(1, 25)]
    out = L.earlier_calls_block(hist, summarise=_summ, max_chars=900)
    assert len(out) <= 900
    assert "call 20:" in out                                   # the newest older call survives
    assert "earlier call(s) not listed" in out


def test_arguments_come_only_through_the_injected_summariser():
    """The loop passes loop_trace_core.call_summary, which masks secrets. With no
    summariser nothing about the arguments is printed at all."""
    hist = [_h(1, args={"token": "s3cret"})] + [_h(i) for i in range(2, 6)]
    out = L.earlier_calls_block(hist)
    assert "s3cret" not in out and "call 1: exec.bash.run -> ok" in out


def test_a_summariser_that_raises_does_not_take_the_step_down():
    def boom(_):
        raise ValueError("bad args")
    out = L.earlier_calls_block([_h(i) for i in range(1, 7)], summarise=boom)
    assert "call 1: exec.bash.run -> ok" in out


def test_the_executor_puts_the_block_into_the_prompt_it_sends():
    """Wiring, parsed from source (the repo's call-site pattern - no app import).
    The executor must call the formatter on its step `history` with the masking
    summariser, fold the result into `obs`, and do both BEFORE `user_msg` is built
    from `obs`. A formatter nobody splices in is the inert-input trap again."""
    import ast
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "vera" / "dag" / "dag_workshop_capabilities.py")
    tree = ast.parse(src.read_text(encoding="utf-8"))
    ex = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_v5_run_step_inner")
    calls = [n for n in ast.walk(ex) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "earlier_calls_block"]
    assert len(calls) == 1, "the executor must build the earlier-calls block once"
    call = calls[0]
    assert isinstance(call.args[0], ast.Name) and call.args[0].id == "history"
    kw = {k.arg: k.value for k in call.keywords}
    assert getattr(kw.get("summarise"), "id", None) == "_call_summary", (
        "arguments must go through call_summary, which masks secrets")
    splice = [n for n in ast.walk(ex) if isinstance(n, ast.Assign)
              and any(getattr(t, "id", None) == "obs" for t in n.targets)
              and any(isinstance(x, ast.Name) and x.id == "_earlier" for x in ast.walk(n.value))]
    assert len(splice) == 1, "the block must be folded into `obs`"
    user_msg = [n for n in ast.walk(ex) if isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == "user_msg" for t in n.targets)
                and any(isinstance(x, ast.Name) and x.id == "obs" for x in ast.walk(n.value))]
    assert user_msg, "user_msg must be built from obs"
    assert call.lineno < splice[0].lineno < min(u.lineno for u in user_msg)


def test_junk_entries_are_ignored():
    hist = [None, "x", _h(1)] + [_h(i) for i in range(2, 6)]
    assert "call 1:" in L.earlier_calls_block(hist, summarise=_summ)
