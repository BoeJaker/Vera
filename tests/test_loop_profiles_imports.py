"""vera/dag/loop_profiles.py must import: it is the module that registers
loops.run, loops.profiles and loops.profile.

Found live 2026-09-10: commit c2b1766 (2026-09-09) put three JavaScript
`true` literals into Python dicts, so the module raised NameError part-way
through its body - after the log line that said 16 profiles were
registered, before any @capability ran. Prod ran a day with no loops.run
(the suite's loop runner), no loops.profiles (every profile dropdown in
Loop Lab empty, /loops/profiles a 404) and none of the six specialist
profiles that commit added. The critical tier passed because nothing
imported the module. This does, the way the loader does.
"""
import importlib.util
import os

import pytest

pytestmark = pytest.mark.critical

ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))


def test_loop_profiles_imports_and_registers_its_capabilities():
    try:
        from Vera.vera import capability_orchestration as ORCH
    except Exception as e:                                       # pragma: no cover
        pytest.skip("app not importable here: %s" % e)
    if not os.path.realpath(getattr(ORCH, "__file__", "")).startswith(ROOT):
        pytest.skip("app module not importable from THIS checkout here")
    path = os.path.join(ROOT, "vera", "dag", "loop_profiles.py")
    spec = importlib.util.spec_from_file_location("loop_profiles_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                                 # NameError here is the regression
    for name in ("loops.run", "loops.profiles", "loops.profile"):
        assert name in ORCH.CAPABILITY_REGISTRY, name
    ids = [p["id"] for p in mod.list_profiles()]
    for pid in ("planning", "coding", "mesh-edge", "foundry-provisioning", "model-catalog"):
        assert pid in ids, "%s missing from %s" % (pid, ids)
    assert not mod.duplicate_profile_ids(), mod.duplicate_profile_ids()


def test_no_javascript_booleans_in_python():
    import re
    with open(os.path.join(ROOT, "vera", "dag", "loop_profiles.py"), encoding="utf-8") as fh:
        src = fh.read()
    bad = [m.start() for m in re.finditer(r"(?<![\w\"'])(true|false|null)(?![\w\"'])", src)]
    assert not bad, "JS literals at offsets %s" % bad[:5]
