"""The four-minds contract helper takes an optional presentation (desk DO plan, 18 Oct): left out, every contract
is exactly what it was; given, it is carried as contract["presentation"], so minds.switch.list draws as its table
again instead of a guessed one."""
import ast
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vera.minds import contract_core as MC  # noqa: E402

pytestmark = pytest.mark.critical

BEFORE_KEYS = ["canonical_task", "lifecycle", "effects", "output_schema", "approval", "trust", "secrets",
               "filesystem", "network", "tenant", "idempotency", "cancellation", "pagination", "resources", "owner"]


def test_without_presentation_the_contract_is_unchanged():
    c = MC.contract("x.y", effects=["read"], idempotency="idempotent")
    assert list(c) == BEFORE_KEYS                     # same keys, same order: no presentation key at all
    assert c == MC.contract("x.y", effects=["read"], idempotency="idempotent", presentation=None)


def test_a_presentation_is_carried_as_given_and_copied():
    p = {"kind": "table", "map": {"rows": "switches"}}
    c = MC.contract("x.y", effects=["read"], idempotency="idempotent", presentation=p)
    assert c["presentation"] == p and c["presentation"] is not p
    assert [k for k in c if k != "presentation"] == BEFORE_KEYS


def _switch_list_contract():
    src = open(os.path.join(ROOT, "vera", "minds", "switchboard_capabilities.py"), encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "cap_minds_switch_list":
            deco = node.decorator_list[0]
            assert ast.literal_eval(deco.args[0]) == "minds.switch.list"
            call = next(k.value for k in deco.keywords if k.arg == "contract")
            assert ast.unparse(call.func) == "_MC.contract"
            args = [ast.literal_eval(a) for a in call.args]
            kw = {k.arg: ast.literal_eval(k.value) for k in call.keywords}
            return MC.contract(*args, **kw)
    raise AssertionError("cap_minds_switch_list not found")


def test_minds_switch_list_declares_its_presentation_again():
    c = _switch_list_contract()
    assert c["owner"] == "four-minds" and c["effects"] == ["read"]
    p = c["presentation"]
    assert p["kind"] == "table" and p["map"] == {"rows": "switches"}
    assert p["draw"]["columns"][:2] == ["id", "default"] and p["empty"] == "No switches"


def test_the_presentation_passes_the_contract_core_checks():
    from vera import capability_contract_core as CC
    c = _switch_list_contract()
    assert CC.presentation_problems(c["presentation"], c["output_schema"]) == []
