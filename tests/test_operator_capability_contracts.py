"""Capability Contract v2 coverage for the complete Operator-facing surface."""

import ast
from pathlib import Path

import pytest

from vera.capability_contract_core import gate_contracts, project_contract
from Vera.vera import capability_orchestration as runtime_orchestration
from Vera.vera.operator import operator_web_capabilities as _operator_caps  # noqa: F401


pytestmark = pytest.mark.critical


EXPECTED_TASKS = {
    "operator.session.start": "browser.session.start",
    "operator.session.status": "browser.session.inspect",
    "operator.session.close": "browser.session.close",
    "operator.connect.list": "browser.connection.list",
    "operator.connect": "browser.connection.open",
    "operator.observe": "browser.page.observe",
    "operator.read": "browser.page.read",
    "operator.screenshot": "browser.page.capture",
    "operator.act": "browser.action.perform",
    "operator.think": "browser.action.propose",
    "operator.step": "browser.run.step",
    "operator.run": "browser.run.execute",
    "operator.mission.list": "browser.mission.list",
    "operator.mission.run": "browser.mission.execute",
    "docs.build": "documentation.capture.build",
    "docs.assets": "documentation.assets.list",
    "docs.capture": "documentation.directive.capture",
    "docs.gallery": "documentation.gallery.rebuild",
    "operator.capture.start": "browser.capture.start",
    "operator.capture.status": "browser.capture.inspect",
    "operator.capture.stop": "browser.capture.stop",
    "operator.tour.list": "browser.tour.list",
    "operator.tour.run": "browser.tour.execute",
    "operator.test.run": "test.pytest.execute",
    "operator.trace": "browser.run.trace",
    "operator.cancel": "browser.run.cancel",
    "operator.runs": "browser.run.list",
}


def _manifests():
    registry = runtime_orchestration.CAPABILITY_REGISTRY
    return [project_contract(name, registry[name]) for name in EXPECTED_TASKS]


def test_complete_operator_surface_has_gated_contracts():
    registry = runtime_orchestration.CAPABILITY_REGISTRY
    tree = ast.parse(Path(_operator_caps.__file__).read_text(encoding="utf-8"))
    declared = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (isinstance(decorator, ast.Call) and decorator.args
                    and isinstance(decorator.args[0], ast.Constant)
                    and str(decorator.args[0].value).startswith(("operator.", "docs."))):
                declared.add(str(decorator.args[0].value))
    assert declared == set(EXPECTED_TASKS)
    assert declared <= set(registry)

    manifests = _manifests()
    assert gate_contracts(manifests)["ok"] is True
    assert {item["name"]: item["canonical_task"] for item in manifests} == EXPECTED_TASKS


@pytest.mark.parametrize("name", [
    "operator.session.start",
    "operator.connect",
    "operator.act",
    "operator.step",
    "operator.run",
])
def test_browser_execution_declares_network_and_policy_authority(name):
    contract = runtime_orchestration.CAPABILITY_REGISTRY[name]["contract"]
    assert "network" in contract["effects"]
    assert contract["approval"]["status"] != "not_required"
    assert contract["network"]["status"] != "not_required"
    assert contract["idempotency"]["status"] == "non_idempotent"


def test_operator_run_declares_model_external_and_audit_resources():
    contract = runtime_orchestration.CAPABILITY_REGISTRY["operator.run"]["contract"]
    assert {"model", "external_side_effect", "filesystem"} <= set(contract["effects"])
    assert {"browser", "model", "redis"} <= set(contract["resources"]["classes"])
    assert contract["cancellation"]["status"] == "cooperative_between_steps"


def test_read_only_operator_caps_do_not_claim_external_side_effects():
    for name in (
        "operator.session.status", "operator.connect.list", "operator.read",
        "operator.mission.list", "docs.assets", "operator.capture.status",
        "operator.tour.list", "operator.trace", "operator.runs",
    ):
        effects = runtime_orchestration.CAPABILITY_REGISTRY[name]["contract"]["effects"]
        assert "external_side_effect" not in effects
        assert "execute" not in effects


def test_contracts_do_not_replace_native_operator_authority():
    text = Path(_operator_caps.__file__).read_text(encoding="utf-8")
    assert "_policy_for_session" in text
    assert "_safety.evaluate" in text
    assert "_op_set_cancel" in text
    assert "_run_projection.observe" in text
