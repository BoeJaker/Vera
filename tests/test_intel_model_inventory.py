from vera.intel import intel_model_inventory as inventory


def test_projects_actual_nlp_consumers_with_package_and_placement_evidence():
    rows = [{
        "id": "nlp.ner:gliner", "cap": "nlp.ner", "selector": {"task": "gliner"},
        "model": "urchade/gliner_multi-v2.1", "package_id": "pkg:gliner",
        "package_ids": ["pkg:gliner"], "present_on": ["cpu-246"], "loaded_on": [],
    }, {
        "id": "nlp.classify:sentiment3", "cap": "nlp.classify",
        "selector": {"task": "sentiment3"}, "model": "cardiffnlp/twitter-roberta",
        "present_on": ["cpu-247"], "loaded_on": ["cpu-247"],
    }]
    result = inventory.project(rows, {"name": "intel", "roles": {}})
    by_id = {row["consumer_id"]: row for row in result["nlp"]}
    assert by_id["intel.enrich.entities"]["status"] == "verified"
    assert by_id["intel.enrich.entities"]["package_id"] == "pkg:gliner"
    assert by_id["intel.enrich.sentiment"]["status"] == "observed"
    assert by_id["intel.enrich.sentiment"]["gaps"] == ["portable_package_unbound"]
    assert by_id["intel.enrich.qa"]["gaps"] == ["callable_binding_missing"]


def test_projects_effective_llm_roles_without_claiming_package_admission():
    rows = [{"id": "llm.generate:qwen", "cap": "llm.generate", "model": "qwen3.6:35b-a3b",
             "present_on": ["cpu-247"], "loaded_on": ["cpu-247"]}]
    profile = {"name": "intel", "roles": {
        "assess": {"model": "qwen3.6:35b-a3b", "pin": "cpu-247",
                   "job_type": "idle_intel", "deny_gpu": True},
        "brief": {"model": "missing:model", "pin": "cpu-246", "job_type": "idle_intel"},
    }}
    result = inventory.project(rows, profile)
    by_role = {row["role"]: row for row in result["roles"]}
    assert by_role["assess"]["status"] == "observed"
    assert by_role["assess"]["loaded_on"] == ["cpu-247"]
    assert by_role["assess"]["deny_gpu"] is True
    assert "package_id" not in by_role["assess"]
    assert by_role["brief"]["gaps"] == ["callable_provider_missing"]


def test_selector_matching_does_not_conflate_default_ner_with_gliner():
    rows = [{"id": "nlp.ner:default", "cap": "nlp.ner", "selector": {"task": "ner"},
             "present_on": ["cpu-246"], "loaded_on": ["cpu-246"]}]
    result = inventory.project(rows, {"name": "intel", "roles": {}})
    entities = next(row for row in result["nlp"] if row["consumer_id"] == "intel.enrich.entities")
    assert entities["status"] == "unresolved"
    assert entities["gaps"] == ["callable_binding_missing"]
