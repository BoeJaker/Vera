"""Completion gate: actions the GOAL asks for, checked against what the run actually called. Pure.

16-18 Oct 2026 census: the gate judged "complete" because the file register showed the file - but the goal
also said "then run the tests" (build-multifile ended with no test run) or "open it in a browser and report
the text" (operator-serves-its-own-file passed with curl, never operator.run). A file existing proves it
was written, not that the run did what the goal asked with it. An action like these leaves a trace in the
step history, so it is checked there, not by a model's opinion.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Tuple

_RUN_TESTS = re.compile(r"\b(run|execute|re-?run)\s+(the\s+|its\s+|all\s+|those\s+|these\s+)?(unit\s+)?tests?\b"
                        r"|\bpytest\b", re.I)
_BROWSER = re.compile(r"\b(open|view|load|check|verify|look\s+at|render)\b[^.\n]{0,80}?\b(in|with|using)\s+"
                      r"(a|the)\s+(real\s+|headless\s+)?browser\b|\brendered\s+page\b|\bin\s+a\s+real\s+browser\b",
                      re.I)
_TEST_CMD = re.compile(r"\bpytest\b|\bunittest\b|-m\s+pytest|\bnpm\s+(run\s+)?test\b|\bgo\s+test\b|\bcargo\s+test\b",
                       re.I)
_TEST_TOOLS = ("exec.bash.run", "exec.python.run", "evolve.unittest.run", "sandbox.exec")


def required(goal: str) -> List[str]:
    """The checkable actions this goal asks for: 'run_tests', 'browser'."""
    g = str(goal or "")
    out = []
    if _RUN_TESTS.search(g):
        out.append("run_tests")
    if _BROWSER.search(g):
        out.append("browser")
    return out


def calls_of(results: Iterable[Dict[str, Any]]) -> List[Tuple[str, str]]:
    """(tool, args as text) for every call in every step's history."""
    out = []
    for r in results or []:
        for h in (r or {}).get("history") or []:
            if isinstance(h, dict) and h.get("tool"):
                try:
                    a = json.dumps(h.get("args") or {}, default=str)
                except Exception:
                    a = str(h.get("args"))
                out.append((str(h["tool"]), a))
    return out


def done(action: str, calls: Iterable[Tuple[str, str]]) -> bool:
    for tool, args in calls or []:
        if action == "run_tests" and (tool in _TEST_TOOLS or tool.startswith("code.test")) and (
                tool == "evolve.unittest.run" or _TEST_CMD.search(args)):
            return True
        if action == "browser" and (tool == "operator.run" or tool.startswith("operator.run.")
                                    or tool.startswith("operator.session")):
            return True
    return False


def missing(goal: str, results: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """[{action, reason, step}] for every action the goal asks for that no step performed."""
    calls = calls_of(results)
    out = []
    for a in required(goal):
        if done(a, calls):
            continue
        if a == "run_tests":
            out.append({"action": a,
                        "reason": "the goal asks for the tests to be run, and no step ran them",
                        "step": {"title": "Run the tests the goal asks for",
                                 "goal": "Run the tests for the files this run wrote (pytest), and report the "
                                         "pass/fail output. Fix nothing here - only run them and report.\nGOAL: "
                                         + str(goal)[:1500],
                                 "caps": ["exec.bash.run"],
                                 "success": "the tests ran and their pass/fail output is in the result"}})
        else:
            out.append({"action": a,
                        "reason": "the goal asks for a real browser, and operator.run was never called "
                                  "(serving the file and reading it with curl is not a browser)",
                        "step": {"title": "Check it in a real browser",
                                 "goal": "Use operator.run to open the page the goal names in a real headless "
                                         "browser and report what the rendered page shows.\nGOAL: "
                                         + str(goal)[:1500],
                                 "caps": ["operator.run"],
                                 "success": "operator.run opened the page and the visible text is reported"}})
    return out
