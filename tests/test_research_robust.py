"""Research robustness: request timeouts sit inside their wait_for (RF-52), a failed
context-window detection is retried rather than cached as 0 (RF-53), and the thinker's
directive enums are validated (RF-54).

researcher_api has app-level side effects, so the pieces are lifted out of the source
with ast and run against fakes.
"""
import ast
import asyncio
import logging
import os
import re
import time
import types

import pytest

pytestmark = pytest.mark.critical

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "vera", "research", "researcher_api.py")
_TEXT = open(SRC, encoding="utf-8").read()
_TREE = ast.parse(_TEXT)


def _segment(name):
    for n in _TREE.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return ast.get_source_segment(_TEXT, n)
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                return ast.get_source_segment(_TEXT, n)
    raise AssertionError(name + " not found")


def test_no_inner_timeout_exceeds_its_wait_for():
    bad = [(a, b) for a, b in re.findall(r"timeout_secs=(\d+)\),\s*\n\s*timeout=(\d+)", _TEXT)
           if int(a) > int(b)]
    assert not bad


def test_directive_enum():
    ns = {}
    for name in ("_OUTPUT_STYLES", "_DIRECTIVE_DEPTHS", "_directive_enum"):
        exec(_segment(name), ns)
    enum, styles, depths = ns["_directive_enum"], ns["_OUTPUT_STYLES"], ns["_DIRECTIVE_DEPTHS"]
    assert enum("Deep Analysis", styles, "report") == "deep_analysis"
    assert enum("newspaper", styles, "report") == "newspaper"
    assert enum("poem", styles, "report") == "report"
    assert enum(None, depths, "standard") == "standard"
    assert enum(["deep"], depths, "standard") == "standard"
    assert enum("DEEP", depths, "standard") == "deep"


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def _detector(responses):
    """_detect_ctx bound to a fake httpx whose post() pops from `responses`."""
    calls = []

    class Client:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            calls.append(url)
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

    ns = {"httpx": types.SimpleNamespace(AsyncClient=Client), "log": logging.getLogger("t"),
          "time": time, "_MAX_AUTO_CTX": 0, "OllamaInstance": object}
    for name in ("_DETECTED_CTX_CACHE", "_DETECTED_CTX_FAIL", "_DETECT_CTX_RETRY_S", "_detect_ctx"):
        exec(_segment(name), ns)
    return ns, calls


INST = types.SimpleNamespace(base_url="http://n1:11434", model="m", name="n1", ctx_size=0)
OK = _Resp(200, {"model_info": {"general.architecture": "llama", "llama.context_length": 32768}})


def test_failure_is_retried_after_cooldown_not_cached_forever():
    ns, calls = _detector([OSError("down"), OK])
    detect = ns["_detect_ctx"]
    assert asyncio.run(detect(INST)) == 8192          # fallback while unreachable
    assert asyncio.run(detect(INST)) == 8192          # inside the cooldown: no new probe
    assert len(calls) == 1
    key = "http://n1:11434::m"
    ns["_DETECTED_CTX_FAIL"][key] -= ns["_DETECT_CTX_RETRY_S"] + 1
    assert asyncio.run(detect(INST)) == 32768         # retried and detected
    assert len(calls) == 2
    assert ns["_DETECTED_CTX_CACHE"][key] == 32768 and key not in ns["_DETECTED_CTX_FAIL"]


def test_success_is_cached():
    ns, calls = _detector([OK])
    detect = ns["_detect_ctx"]
    assert asyncio.run(detect(INST)) == 32768
    assert asyncio.run(detect(INST)) == 32768
    assert len(calls) == 1
