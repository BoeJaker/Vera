"""The stall dumper must name the frame someone can act on, not a stdlib line.

Pins vera.monitor.stall_trace_core. The regression (2026-09-16, prod): the
"where" for a loop-stall dump was the deepest frame not under /site-packages/.
The standard library is not under site-packages either, so a 1561ms stall
inside cap_sbx_list's json.loads was reported as `decoder.py:353`, a stall
interrupted by a GC weakref callback as `_weakrefset.py:39`, and a loop blocked
in native code as `runners.py:118`. perf.scan then printed those three as
"Blocking call(s)", which is where an investigation starts — and none of them
was the culprit. The Vera frame naming the real blocking call
(session_sandbox_capabilities.py:1418) sat directly above json.loads in two of
those same stacks and was never shown.

The stacks below are the real ones captured from prod that day.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.monitor.stall_trace_core import (  # noqa: E402
    NO_APP_FRAME, parse_frames, stall_where,
)

PKG = "/home/boejaker/Vera/vera"

# Captured 2026-09-16 09:33:00Z — 1561ms. Old rule said decoder.py:353.
STACK_JSON_LOADS = '''  File "<frozen runpy>", line 198, in _run_module_as_main
  File "/home/boejaker/Vera/vera/capability_orchestration.py", line 11461, in <module>
    uvicorn.run("Vera.vera.capability_orchestration:APP",
  File "/home/boejaker/langchain/lib/python3.11/site-packages/uvicorn/main.py", line 577, in run
    server.run()
  File "/usr/lib/python3.11/asyncio/runners.py", line 118, in run
    return self._loop.run_until_complete(task)
  File "/home/boejaker/Vera/vera/capability_orchestration.py", line 10142, in _handler
    result = await cap["func"](**coerced, trace_id=new_id())
  File "/home/boejaker/Vera/vera/remote/session_sandbox_capabilities.py", line 1418, in cap_sbx_list
    recs.append(json.loads(v))
  File "/usr/lib/python3.11/json/__init__.py", line 346, in loads
    return _default_decoder.decode(s)
  File "/usr/lib/python3.11/json/decoder.py", line 353, in raw_decode
    obj, end = self.scan_once(s, idx)
'''

# Captured 2026-09-16 09:35:39Z — 2124ms, the worst of the window.
# Old rule said _weakrefset.py:39 (a GC weakref callback).
STACK_GC_UNDER_JSON = '''  File "<frozen runpy>", line 198, in _run_module_as_main
  File "/usr/lib/python3.11/asyncio/runners.py", line 118, in run
    return self._loop.run_until_complete(task)
  File "/home/boejaker/Vera/vera/capability_orchestration.py", line 7670, in _cap_call
    return await cap["func"](**kw)
  File "/home/boejaker/Vera/vera/workers/syslog.py", line 487, in _patched
    result = await orig(**kw)
  File "/home/boejaker/Vera/vera/remote/session_sandbox_capabilities.py", line 1418, in cap_sbx_list
    recs.append(json.loads(v))
  File "/usr/lib/python3.11/json/__init__.py", line 346, in loads
    return _default_decoder.decode(s)
  File "/usr/lib/python3.11/_weakrefset.py", line 39, in _remove
    def _remove(item, selfref=ref(self)):
'''

# Captured 2026-09-16 09:30:56Z — 1094ms. No application frame at all: the main
# thread was not executing Python. Old rule said runners.py:118.
STACK_NO_APP_FRAME = '''  File "<frozen runpy>", line 198, in _run_module_as_main
  File "/home/boejaker/Vera/vera/capability_orchestration.py", line 11461, in <module>
    uvicorn.run("Vera.vera.capability_orchestration:APP",
  File "/home/boejaker/langchain/lib/python3.11/site-packages/uvicorn/server.py", line 65, in run
    return asyncio.run(self.serve(sockets=sockets))
  File "/usr/lib/python3.11/asyncio/runners.py", line 190, in run
    return runner.run(main)
  File "/usr/lib/python3.11/asyncio/runners.py", line 118, in run
    return self._loop.run_until_complete(task)
'''

# The GC pacer calling gc.collect() IS a Vera frame and should be named.
STACK_GC_PACER = '''  File "/usr/lib/python3.11/asyncio/runners.py", line 118, in run
    return self._loop.run_until_complete(task)
  File "/home/boejaker/Vera/vera/capability_orchestration.py", line 9580, in _gc_pacer
    n = gc.collect() if full else gc.collect(1)
  File "/usr/lib/python3.11/_weakrefset.py", line 39, in _remove
    def _remove(item, selfref=ref(self)):
'''


def test_json_loads_stall_names_the_vera_frame_not_the_decoder():
    # The whole point: this stall is cap_sbx_list's, not json's.
    assert stall_where(STACK_JSON_LOADS, PKG) == "session_sandbox_capabilities.py:1418"


def test_gc_callback_under_json_still_names_the_vera_frame():
    assert stall_where(STACK_GC_UNDER_JSON, PKG) == "session_sandbox_capabilities.py:1418"


def test_gc_pacer_is_itself_a_vera_frame():
    assert stall_where(STACK_GC_PACER, PKG) == "capability_orchestration.py:9580"


def test_stack_with_no_app_frame_says_so_instead_of_naming_stdlib():
    # Naming runners.py:118 sent the last investigation at asyncio's own stack.
    # No application frame is a DIFFERENT diagnosis (GIL/CPU starvation).
    where = stall_where(STACK_NO_APP_FRAME, PKG)
    assert where == NO_APP_FRAME
    assert "runners.py" not in where


def test_old_rule_would_have_picked_stdlib():
    """Guards the actual regression: deepest-non-site-packages picks stdlib."""
    def old_rule(stack):
        for ln in reversed(stack.splitlines()):
            s = ln.strip()
            if s.startswith('File "') and "/site-packages/" not in s:
                path = s.replace('File "', "").split('"')[0]
                lno = s.split("line ", 1)[1].split(",")[0]
                return f"{path.split('/')[-1]}:{lno}"
        return ""

    assert old_rule(STACK_JSON_LOADS) == "decoder.py:353"
    assert old_rule(STACK_GC_UNDER_JSON) == "_weakrefset.py:39"
    assert old_rule(STACK_NO_APP_FRAME) == "runners.py:118"
    # ...and the new rule disagrees with all three.
    for stack in (STACK_JSON_LOADS, STACK_GC_UNDER_JSON, STACK_NO_APP_FRAME):
        assert stall_where(stack, PKG) != old_rule(stack)


def test_container_package_path_also_matches():
    """Prod runs from /home/boejaker/Vera/vera, a sandbox from /app/Vera/vera."""
    stack = STACK_JSON_LOADS.replace("/home/boejaker/Vera/vera", "/app/Vera/vera")
    assert stall_where(stack, "/app/Vera/vera") == "session_sandbox_capabilities.py:1418"


def test_unknown_package_dir_falls_back_to_the_non_stdlib_frame():
    # With no package dir we cannot prefer Vera frames, but we must still skip
    # stdlib and site-packages rather than returning the deepest line blindly.
    assert stall_where(STACK_JSON_LOADS, None) == "session_sandbox_capabilities.py:1418"


def test_empty_and_garbage_inputs_do_not_raise():
    assert stall_where("", PKG) == ""
    assert stall_where("not a traceback at all", PKG) == ""
    assert parse_frames("") == []
