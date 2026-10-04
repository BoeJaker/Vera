"""The loop never lets its executor switch the writer's reasoning pass on.

4 Oct 2026: prose.author defaults think OFF, but `think` is in its schema and the loop's
executor passed think=true on 37 of 207 calls. The writer model then reasons until its
token budget runs out - 3,121 tokens for 977 chars; a 4,000-token cap hit and the report
came back "INCOMPLETE". Direct probe the same day: think off 95 tokens / 2.8 s; on or
unset 2,500 tokens of reasoning and no answer at all."""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / "vera" / "dag" / "dag_workshop_capabilities.py").read_text(encoding="utf-8")


def _block():
    i = SRC.index("for _fld, _val, _note in _v5_heal_author_args(tool, args, step):")
    return SRC[i:i + 2600]


def test_the_loop_drops_an_executor_set_think_for_prose_author():
    b = _block()
    assert 'if tool == "prose.author" and isinstance(args, dict) and "think" in args:' in b
    assert '_dropped_think = args.pop("think")' in b
    assert "agent_loop_v5.arg_correction" in b[b.index("_dropped_think"):]


def test_it_runs_before_the_writer_gets_its_sources():
    b = _block()
    assert b.index('args.pop("think")') < b.index("_research_sources_core.wants_sources(")


def test_prose_author_still_defaults_thinking_off_and_a_direct_caller_may_ask():
    cap = SRC[SRC.index("async def cap_prose_author("):]
    sig = cap[:cap.index("):")]
    assert "think: Optional[bool] = None" in sig
    assert cap.count("think=(False if think is None else think)") >= 1
