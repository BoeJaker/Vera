"""Finalize and the journal share one model call per step (four-minds Q2 #6, 2026-10-08).

With the journal on, finalize's pass returns the finalised result and the journal fields
together; the journal uses them and makes no call of its own. Everything else keeps the two
separate passes: journal off, finalize skipped, or an unreadable reply.
"""
import ast
import asyncio
import json
import logging
import os

import pytest

pytestmark = pytest.mark.critical

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "vera", "dag", "dag_workshop_capabilities.py")
TEXT = open(SRC, encoding="utf-8").read()
TREE = ast.parse(TEXT)
SPEC = next(n for n in TREE.body if isinstance(n, ast.Assign)
            and getattr(n.targets[0], "id", "") == "_V6_JOURNAL_FIELDS_SPEC")


def _load(name, generate):
    fn = next(n for n in TREE.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)

    async def no_paths(sid, paths):
        return {}
    import typing
    ns = {"Dict": typing.Dict, "Any": typing.Any, "List": typing.List, "Optional": typing.Optional,
          "log": logging.getLogger("t"),
          "_V6_FINALIZE_MIN_RAW": 10, "_V6_FINALIZE_OUT_MAX": 4000, "_V5_DONE_SUMMARY": 6000,
          "_V5_UTILITY_TIMEOUT": 30, "_v6_authored_path": lambda h: "", "_strip_think": lambda s: (s, ""),
          "_extract_json": lambda s: json.loads(s), "_v6_extract_paths": lambda s: [],
          "_v6_check_paths_exist": no_paths, "_safe_ollama_generate_dw": generate,
          "_v6_journal_derive": lambda res: {"files": [], "tools": ["web.research"]},
          "now_iso": lambda: "2026-10-08T00:00:00Z"}
    exec(compile(ast.get_source_segment(TEXT, SPEC), SRC, "exec"), ns)
    exec(compile(ast.get_source_segment(TEXT, fn), SRC, "exec"), ns)
    return ns[name]


RAW = "Searched three sources. The 2024 SSPL change applies to Redis 7.4+. Retried once. " * 3
STEP = {"id": 1, "title": "check licence", "goal": "find the redis licence", "success": "licence named"}


def test_with_the_journal_on_one_call_serves_both():
    calls = []

    async def gen(prompt, system="", json_mode=False, **k):
        calls.append(json_mode)
        return json.dumps({"result": "Redis 7.4+ is under the SSPL (2024 change).",
                           "key_outputs": ["SSPL from 7.4"], "files": [], "entities": ["Redis"],
                           "schema": "", "note": "licence identified"})
    res = {"ok": True, "summary": RAW, "history": [{"tool": "web.research", "ok": True}]}
    asyncio.run(_load("_v6_finalize_step", gen)(STEP, res, "goal", model="m", instance_id="",
                                                  prefer_gpu=False, with_journal=True))
    assert res["finalized"] and res["summary"].startswith("Redis 7.4+") and res["raw_summary"] == RAW
    assert calls == [True]

    async def no_model(*a, **k):
        raise AssertionError("the journal made a second call")
    rec = asyncio.run(_load("_v6_journal_extract", no_model)(STEP, res, "goal", model="m",
                                                             instance_id="", prefer_gpu=False))
    assert rec["source"] == "finalize" and rec["key_outputs"] == ["SSPL from 7.4"]
    assert rec["entities"] == ["Redis"] and rec["note"] == "licence identified"


def test_with_the_journal_off_finalize_is_plain_text_and_adds_nothing():
    calls = []

    async def gen(prompt, system="", json_mode=False, **k):
        calls.append(json_mode)
        return "Redis 7.4+ is under the SSPL (2024 change)."
    res = {"ok": True, "summary": RAW, "history": []}
    asyncio.run(_load("_v6_finalize_step", gen)(STEP, res, "goal", model="m", instance_id="",
                                                  prefer_gpu=False))
    assert calls == [False] and res["finalized"] and "journal_extract" not in res


def test_an_unreadable_reply_keeps_the_raw_summary_and_the_journal_calls_itself():
    async def gen(prompt, system="", json_mode=False, **k):
        return '{"result": ""}'
    res = {"ok": True, "summary": RAW, "history": []}
    asyncio.run(_load("_v6_finalize_step", gen)(STEP, res, "goal", model="m", instance_id="",
                                                  prefer_gpu=False, with_journal=True))
    assert res["summary"] == RAW and "journal_extract" not in res and not res.get("finalized")
    called = []

    async def journal_model(prompt, system="", json_mode=False, **k):
        called.append(1)
        return json.dumps({"key_outputs": ["x"], "files": [], "entities": [], "schema": "", "note": "n"})
    rec = asyncio.run(_load("_v6_journal_extract", journal_model)(STEP, res, "goal", model="m",
                                                                  instance_id="", prefer_gpu=False))
    assert called == [1] and rec["source"] == "extract"


def test_the_runner_asks_for_the_journal_fields_when_it_keeps_a_journal():
    assert "session_id=sid, with_journal=journal_on)" in TEXT
