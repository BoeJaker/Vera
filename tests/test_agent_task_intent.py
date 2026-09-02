import json
from pathlib import Path

import pytest

from vera.agent_task_intent import (
    DOCUMENT_AUTHOR_TASK,
    SOURCE_AUTHOR_TASK,
    infer_authoring_tasks,
    public_task_resolution,
    resolve_authoring_tasks,
    route_authoring_caps,
)
from vera.dag import dag_workshop_capabilities as workshop


pytestmark = pytest.mark.critical


@pytest.mark.parametrize(("text", "mode", "tasks"), [
    ("Create app.py with the parser", "resolved", [SOURCE_AUTHOR_TASK]),
    ("Write the final findings report.md", "resolved", [DOCUMENT_AUTHOR_TASK]),
    ("Build app.py and write its README", "compound",
     [SOURCE_AUTHOR_TASK, DOCUMENT_AUTHOR_TASK]),
    ("Investigate the failure", "ambiguous", []),
])
def test_authoring_intent_is_deterministic_and_content_free(text, mode, tasks):
    result = infer_authoring_tasks(text)
    assert result["mode"] == mode
    assert result["canonical_tasks"] == tasks
    assert text not in json.dumps(result)
    assert result["authorized"] is False and result["executed"] is False


def _resolve(text):
    catalog = ["code.author", "prose.author", "llm.generate", "exec.bash.run"]
    return resolve_authoring_tasks(text, catalog, workshop.CAPABILITY_REGISTRY)


def test_contract_resolver_selects_grounded_provider_without_execution():
    source = _resolve("Implement parser.py")
    assert source["selections"] == [{
        "canonical_task": SOURCE_AUTHOR_TASK,
        "selected": "code.author", "alternatives": [], "status": "resolved"}]
    public = public_task_resolution(source)
    assert public["authorized"] is False and public["executed"] is False
    assert "provider_tasks" not in public

    document = _resolve("Produce the final report")
    assert document["selections"][0]["selected"] == "prose.author"


def test_routing_replaces_wrong_author_family_and_raw_generation_peer():
    source = _resolve("Create a Python module")
    assert route_authoring_caps(
        ["prose.author", "llm.generate", "exec.bash.run"], source) == [
            "code.author", "exec.bash.run"]
    document = _resolve("Write a findings report")
    assert route_authoring_caps(
        ["code.author", "llm.generate", "exec.bash.run"], document) == [
            "prose.author", "exec.bash.run"]


def test_ambiguous_and_unavailable_resolution_fail_open():
    ambiguous = _resolve("Inspect the current state")
    original = ["code.author", "prose.author", "exec.bash.run"]
    assert route_authoring_caps(original, ambiguous) == original
    unavailable = resolve_authoring_tasks(
        "Write a report", ["exec.bash.run"], workshop.CAPABILITY_REGISTRY)
    assert unavailable["selections"][0]["status"] == "unavailable"
    assert route_authoring_caps(original, unavailable) == original


def test_step_coercion_records_task_contract_and_routes_exact_provider():
    catalog = ["code.author", "prose.author", "llm.generate", "exec.bash.run"]
    code = workshop._v5_coerce_step(
        {"title": "Implement parser.py", "goal": "Create the Python parser module",
         "caps": ["prose.author", "llm.generate", "exec.bash.run"]},
        0, "Build a parser", catalog, set(catalog), set())
    assert code["caps"][0] == "code.author"
    assert "prose.author" not in code["caps"]
    assert "llm.generate" not in code["caps"]
    # The serving sandbox's partial test registry omits exec.bash.run while the
    # fresh critical-gate registry includes it. In the latter case routing must
    # preserve that unrelated, admitted capability.
    if "exec.bash.run" in workshop.CAPABILITY_REGISTRY:
        assert "exec.bash.run" in code["caps"]
    assert code["task_resolution"]["canonical_tasks"] == [SOURCE_AUTHOR_TASK]

    doc = workshop._v5_coerce_step(
        {"title": "Write report.md", "goal": "Write the final findings report",
         "caps": ["code.author", "llm.generate"]},
        0, "Deliver findings", catalog, set(catalog), set())
    assert doc["caps"] == ["prose.author"]
    assert doc["task_resolution"]["canonical_tasks"] == [DOCUMENT_AUTHOR_TASK]


def test_step_event_and_ui_expose_only_content_free_task_routing():
    root = Path(__file__).parents[1]
    source = (root / "vera" / "dag" / "dag_workshop_capabilities.py").read_text()
    ui = (root / "vera" / "agent_loop_ouput.js").read_text()
    assert source.count('"task_resolution": step.get("task_resolution")') >= 3
    assert '"task_resolution": cstep.get("task_resolution")' in source
    assert "task routing:" in ui
    assert "source_file.author" in ui and "document.author" in ui
