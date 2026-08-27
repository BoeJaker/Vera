"""Regression guard for pipeline.begin -> test -> promote on the Vera repo."""

import ast
from pathlib import Path

import pytest


pytestmark = pytest.mark.critical


def _pipeline_test_node():
    source = Path("vera/evolve/evolve_capabilities.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    return next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                and node.name == "evolve_pipeline_test")


def test_vera_pipeline_test_runs_isolated_critical_gate_instead_of_refusing():
    node = _pipeline_test_node()
    calls = {child.func.id for child in ast.walk(node)
             if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)}
    messages = {child.value for child in ast.walk(node)
                if isinstance(child, ast.Constant) and isinstance(child.value, str)}
    assert "evolve_unittest_run" in calls
    assert not any("repo != 'vera'" in message for message in messages)


def test_vera_pipeline_gate_records_compile_and_critical_outcomes():
    node = _pipeline_test_node()
    assigned = {target.id for child in ast.walk(node)
                if isinstance(child, ast.Assign)
                for target in child.targets if isinstance(target, ast.Name)}
    assert {"compile_ok", "critical_ok", "passed"}.issubset(assigned)
