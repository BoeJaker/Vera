from pathlib import Path

import pytest

from vera.models.model_inventory import project_model_inventory
from vera.models.model_package import ModelArtifact, ModelCompatibility, ModelPackage
from vera.models.model_package_store import SQLiteModelPackageRegistry


pytestmark = pytest.mark.critical


def _package(name="ner-model"):
    return ModelPackage(
        name=name,
        version="content-abc123",
        architecture="roberta",
        format="onnx",
        artifacts=(ModelArtifact(
            "model", f"model-store://{name}/model.onnx", "a" * 64, 42),),
        compatibility=ModelCompatibility(
            tasks=("ner",),
            input_contract="vera.nlp.text/v1",
            output_contract="vera.nlp.ner/v1",
            accelerators=("cpu",),
        ),
        framework="onnxruntime",
    )


def test_inventory_joins_package_evidence_without_executing_it():
    package = _package()
    result = project_model_inventory(
        packages=(package,),
        aliases=({"alias": "current-ner", "package_id": package.package_id},),
        admissions=({"admission_id": "adm_1", "package_id": package.package_id},),
        deployments=({
            "deployment_id": "idep_1", "package_id": package.package_id,
            "provider_id": "onnx-cpu",
        },),
        observations=({
            "deployment_id": "idep_1", "observation_id": "idobs_1",
            "observed_state": "ready",
        },),
        providers=({
            "provider_id": "onnx-cpu", "package_ids": [package.package_id],
            "tasks": ["ner"], "state": "ready",
        },),
        legacy_bindings=({
            "capability": "nlp.ner", "selector": "", "package_id": package.package_id,
        },),
        source_status={"registry": {"status": "available"}},
    )

    assert result["schema"] == "vera.model-inventory/v1"
    assert result["counts"] == {
        "packages": 1, "candidates": 0, "aliases": 1, "deployments": 1,
        "providers": 1, "conflicts": 0, "external_sources": 0,
    }
    entry = result["packages"][0]
    assert entry["package"] == package.to_dict()
    assert entry["sources"] == ["registry"]
    assert entry["aliases"] == ["current-ner"]
    assert entry["deployments"][0]["observation"]["observed_state"] == "ready"
    assert entry["providers"][0]["provider_id"] == "onnx-cpu"
    assert entry["legacy_bindings"][0]["capability"] == "nlp.ner"


def test_nlp_packages_are_deduplicated_and_candidates_remain_unresolved():
    package = _package()
    external = {
        "schema": "vera.nlp-model-inventory/v1",
        "packages": [package.to_dict()],
        "candidates": [{
            "task": "classify", "model": "owner/legacy", "format": "onnx",
            "status": "unresolved", "blockers": ["missing_content_verified_manifest"],
        }],
        "conflicts": [],
    }
    result = project_model_inventory(
        packages=(package,), external_inventories=(("nlp", external),))

    assert result["counts"]["packages"] == 1
    assert result["packages"][0]["sources"] == ["nlp", "registry"]
    assert result["candidates"] == [{
        "source": "nlp", "task": "classify", "model": "owner/legacy",
        "format": "onnx", "status": "unresolved",
        "blockers": ["missing_content_verified_manifest"],
    }]


def test_external_inventory_fails_closed_on_bad_schema_and_package():
    result = project_model_inventory(external_inventories=(
        ("unknown", {"schema": "other/v1", "packages": [_package().to_dict()]}),
        ("nlp", {"schema": "vera.nlp-model-inventory/v1", "packages": [
            {"schema": "vera.model-package/v1", "package_id": "invented"}
        ]}),
    ))

    assert result["packages"] == []
    assert [item["kind"] for item in result["conflicts"]] == [
        "invalid_package", "unsupported_external_inventory"]


def test_dangling_references_are_visible_not_silently_dropped():
    result = project_model_inventory(
        aliases=({"alias": "current", "package_id": "mpkg_missing"},),
        deployments=({"deployment_id": "idep_1", "package_id": "mpkg_missing"},),
        providers=({"provider_id": "provider", "package_ids": ["mpkg_missing"]},),
    )
    assert result["counts"]["packages"] == 0
    assert {item["kind"] for item in result["conflicts"]} == {
        "dangling_alias", "unknown_package_reference", "unknown_provider_package"}


def test_sqlite_registry_lists_aliases_stably(tmp_path):
    store = SQLiteModelPackageRegistry(tmp_path / "registry.sqlite")
    first, second = _package("a"), _package("b")
    store.register(second)
    store.register(first)
    store.alias("z", second.package_id)
    store.alias("a", first.package_id)
    assert store.aliases() == (
        {"alias": "a", "package_id": first.package_id},
        {"alias": "z", "package_id": second.package_id},
    )


def test_nlp_panel_exposes_read_only_portable_inventory():
    panel = Path("vera/research/nlp_panel.html").read_text(encoding="utf-8")
    assert 'id="model-inventory-btn"' in panel
    assert "'/models/inventory'" in panel
    assert "result.schema !== 'vera.model-inventory/v1'" in panel
    assert "this view does not load or execute models" in panel


def test_inventory_capability_loads_after_nlp_discovery():
    source = Path("vera/capability_orchestration.py").read_text(encoding="utf-8")
    dispatch = source.index('"research/nlp_dispatch.py"')
    nlp = source.index('"research/nlp_capabilities.py"')
    inventory = source.index('"models/model_inventory_capabilities.py"')
    assert dispatch < nlp < inventory
