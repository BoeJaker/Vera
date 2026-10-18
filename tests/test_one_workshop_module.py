"""S16 / D12-4: the loop engine module exists once, so the loop recursion blacklist extensions reach the copy that runs.

Importing dag_workshop_capabilities by its full name used to execute the file a second time; that copy re-registered
dag.agent_loop_v6/v7 and ran without loops.run (and the v8, evolve and sandbox caps) in its blacklist.

Runs in the gate container, where Vera.vera resolves to the branch."""
import importlib
import os
import sys

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.join(os.path.dirname(__file__), "..")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import Vera.vera.dag.dag_workshop_capabilities as dw  # noqa: E402  (fails loudly, never skips)
from Vera.vera.capability_orchestration import CAPABILITY_REGISTRY  # noqa: E402


def test_both_names_are_one_module_object():
    assert sys.modules["dag_workshop_capabilities"] is dw
    assert sys.modules["Vera.vera.dag.dag_workshop_capabilities"] is dw
    assert importlib.import_module("vera.dag.dag_workshop_capabilities") is dw     # the worktree-relative name too
    assert importlib.import_module("Vera.vera.dag.dag_workshop_capabilities") is dw
    from Vera.vera.dag import dag_workshop_capabilities as again
    assert again is dw


def test_the_loader_would_skip_it_now():
    # the module loader skips a file whose bare name is already in sys.modules (capability_orchestration)
    assert "dag_workshop_capabilities" in sys.modules


def test_the_registered_loop_engine_is_the_one_whose_blacklist_is_extended():
    import Vera.vera.dag.loop_profiles  # noqa: F401  (extends the blacklist of the module under the bare name)
    import inspect
    raw = inspect.unwrap(CAPABILITY_REGISTRY["dag.agent_loop_v6"]["raw"])
    assert raw.__globals__ is dw.__dict__                  # the registry runs THIS copy
    assert "loops.run" in dw._DEFAULT_CAP_BLACKLIST
    assert "loops.run" in raw.__globals__["_DEFAULT_CAP_BLACKLIST"]
