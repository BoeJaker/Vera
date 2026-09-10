"""edge_registry: the named integration branches Loop Lab lands on.

Pure module, no app boot. Pins the contract every other site relies on: the
default edge is still `bleeding-edge`; a second edge is protected everywhere the
moment it is registered; the hooks' protected-branches file agrees with the
registry; an edge can be added from the environment without a code change.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from vera.evolve import edge_registry as er  # noqa: E402
from vera.evolve import sandbox_reap  # noqa: E402
from vera.evolve import sandbox_lifecycle  # noqa: E402
from vera.evolve import evolve_git_core as core  # noqa: E402

NO_ENV = {}


def test_default_edge_is_bleeding_edge():
    d = er.resolve_edge("", env=NO_ENV)
    assert d["name"] == "bleeding-edge"
    assert d["branch"] == "bleeding-edge"
    assert d["mirror"] == "loop-lab/bleeding-edge-mirror"
    assert d["slug"] == "bleeding-edge-mirror"          # what _safe_branch makes of the mirror
    assert d["default"] is True
    assert er.edge_names(env=NO_ENV)[0] == "bleeding-edge"


def test_design_edge_is_registered_and_derived():
    d = er.resolve_edge("bleeding-edge-design", env=NO_ENV)
    assert d and d["branch"] == "bleeding-edge-design"
    assert d["mirror"] == "loop-lab/bleeding-edge-design-mirror"
    assert d["slug"] == "bleeding-edge-design-mirror"
    assert d["base"] == "main"
    assert d["default"] is False


def test_resolve_by_branch_mirror_slug_and_unknown():
    assert er.resolve_edge("loop-lab/bleeding-edge-mirror", env=NO_ENV)["name"] == "bleeding-edge"
    assert er.resolve_edge("bleeding-edge-design-mirror", env=NO_ENV)["name"] == "bleeding-edge-design"
    assert er.resolve_edge("BLEEDING-EDGE", env=NO_ENV)["name"] == "bleeding-edge"
    assert er.resolve_edge("feat/anything", env=NO_ENV) is None
    assert er.resolve_edge("main", env=NO_ENV) is None


def test_edge_for_branch_is_exact():
    assert er.edge_for_branch("bleeding-edge", env=NO_ENV)["name"] == "bleeding-edge"
    assert er.edge_for_branch("bleeding-edge-design", env=NO_ENV)["name"] == "bleeding-edge-design"
    assert er.edge_for_branch("loop-lab/bleeding-edge-mirror", env=NO_ENV) is None
    assert er.edge_for_branch("", env=NO_ENV) is None
    assert er.is_edge_mirror("loop-lab/bleeding-edge-design-mirror", env=NO_ENV)
    assert not er.is_edge_mirror("bleeding-edge-design", env=NO_ENV)


def test_protected_set_covers_every_edge_and_mirror():
    prot = er.edge_protected_branches(env=NO_ENV)
    for name in ("main", "master", "loop-lab/mainline-mirror",
                 "bleeding-edge", "loop-lab/bleeding-edge-mirror",
                 "bleeding-edge-design", "loop-lab/bleeding-edge-design-mirror"):
        assert name in prot, name


def test_reaper_and_lifecycle_guard_protect_the_design_edge():
    # the incident this guards against: a "merged-looking" trunk force-deleted by the sweep
    assert sandbox_reap.is_trunk_protected("bleeding-edge-design")
    assert sandbox_reap.is_trunk_protected("loop-lab/bleeding-edge-design-mirror")
    assert sandbox_reap.is_trunk_protected("bleeding-edge")
    assert "bleeding-edge-design" in sandbox_lifecycle.PROTECTED_BRANCHES
    assert "loop-lab/bleeding-edge-design-mirror" in sandbox_lifecycle.PROTECTED_BRANCHES
    assert "bleeding-edge" in sandbox_lifecycle.PROTECTED_BRANCHES


def test_hooks_file_agrees_with_registry():
    names = er.read_protected_branches_file(os.path.join(ROOT, "tools", "hooks", "protected-branches"))
    assert names, "tools/hooks/protected-branches is missing or empty"
    drift = er.protected_file_drift(names, env=NO_ENV)
    assert drift == {"missing": [], "extra": []}, drift


def test_env_declares_an_extra_edge_without_code():
    env = {er._ENV_VAR: "bleeding-edge-ops=bleeding-edge-ops:main, bad, x="}
    reg = er.edges(env=env)
    assert "bleeding-edge-ops" in reg
    assert reg["bleeding-edge-ops"]["mirror"] == "loop-lab/bleeding-edge-ops-mirror"
    assert "bleeding-edge-ops" in er.edge_protected_branches(env=env)
    assert er.hook_protected_names(env=env)[-1] == "bleeding-edge-ops"
    # the built-ins are untouched
    assert reg["bleeding-edge"]["default"] is True


def test_branch_prefix_matches_capabilities_module():
    # evolve_capabilities.BRANCH_PREFIX is what _safe_branch strips; the registry
    # derives slugs the same way, so the two must agree
    src = open(os.path.join(ROOT, "vera", "evolve", "evolve_capabilities.py"), encoding="utf-8").read()
    assert 'BRANCH_PREFIX = "%s"' % er.BRANCH_PREFIX in src


def test_git_core_messages_name_the_callers_edge():
    msg = core.main_merge_refusal("main", "main", "", edge_name="bleeding-edge-design")
    assert "bleeding-edge-design" in msg
    assert core.main_merge_refusal("bleeding-edge-design", "main", "") == ""
    got = core.release_preflight("main", "edge", False, True, edge_name="bleeding-edge-design")
    assert got["ok"] is False and "bleeding-edge-design" in got["error"]
    # the defaults still read as they always did
    assert "bleeding-edge" in core.main_merge_refusal("main", "main", "")
    assert "bleeding-edge" in core.release_preflight("main", "edge", False, False)["error"]
