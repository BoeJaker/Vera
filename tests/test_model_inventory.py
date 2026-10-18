from pathlib import Path

import pytest

from vera.models.inference_deployment import (
    SQLiteInferenceDeploymentRegistry,
    define_inference_deployment,
)
from vera.models.inference_health import InferenceProviderHealth
from vera.models.model_inventory import (
    project_model_inventory,
    project_persisted_inference_providers,
)
from vera.models.nlp_inventory import project_nlp_inventory
from vera.models.admission import (
    ModelDeploymentTarget,
    ModelTrustPolicy,
    evaluate_model_admission,
)
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
        metadata=(("model_id", name),),
    )


def test_inventory_joins_package_evidence_without_executing_it():
    package = _package()
    result = project_model_inventory(
        packages=(package,),
        aliases=({"alias": "current-ner", "package_id": package.package_id},),
        admissions=({"admission_id": "adm_1", "package_id": package.package_id},),
        activations=({
            "sequence": 1, "operation_id": "activate_1",
            "package_id": package.package_id,
        },),
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
        "providers": 1, "source_evidence": 0, "conflicts": 0,
        "external_sources": 0,
    }
    entry = result["packages"][0]
    assert entry["package"] == package.to_dict()
    assert entry["sources"] == ["registry"]
    assert entry["aliases"] == ["current-ner"]
    assert entry["deployments"][0]["observation"]["observed_state"] == "ready"
    assert entry["activations"][0]["operation_id"] == "activate_1"
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


def test_nlp_verified_package_retains_source_owned_node_evidence():
    package = _package()
    nodes = (
        {"node_id": "cpu-b", "tasks": {"ner": {
            "model": "ner-model", "present": True, "loaded": False,
            "model_package": package.to_dict(),
        }}},
        {"node_id": "cpu-a", "tasks": {"ner": {
            "model": "ner-model", "present": True, "loaded": True,
            "model_package": package.to_dict(),
        }}},
    )
    external = project_nlp_inventory(nodes)
    result = project_model_inventory(
        external_inventories=(("nlp", external),))

    assert external["counts"] == {
        "packages": 1, "package_observations": 1, "candidates": 0,
    }
    assert result["counts"]["source_evidence"] == 1
    assert result["counts"]["deployments"] == 0
    assert result["packages"][0]["source_evidence"] == [{
        "source": "nlp",
        "schema": "vera.nlp-package-observation/v1",
        "package_id": package.package_id,
        "task": "ner",
        "model": "ner-model",
        "present_on": ["cpu-a", "cpu-b"],
        "loaded_on": ["cpu-a"],
    }]


def test_nlp_unresolved_candidate_converges_specialist_placement_facts():
    result = project_nlp_inventory((
        {"node_id": "gpu", "tasks": {"nli": {
            "model": "cross-encoder/nli", "present": True, "loaded": True,
        }}},
        {"node_id": "cpu", "tasks": {"nli": {
            "model": "cross-encoder/nli", "present": True, "loaded": False,
        }}},
    ))

    assert result["packages"] == []
    assert result["package_observations"] == []
    assert result["candidates"] == [{
        "task": "nli", "model": "cross-encoder/nli", "format": "onnx",
        "present_on": ["cpu", "gpu"], "loaded_on": ["gpu"],
        "blockers": ["missing_content_verified_manifest"],
        "status": "unresolved",
    }]


def test_external_package_observation_fails_closed_without_known_package():
    result = project_model_inventory(external_inventories=(("nlp", {
        "schema": "vera.nlp-model-inventory/v1",
        "packages": [], "candidates": [],
        "package_observations": [{
            "schema": "vera.nlp-package-observation/v1",
            "package_id": "mpkg_missing", "task": "ner", "model": "missing",
        }],
    }),))

    assert result["packages"] == []
    assert result["counts"]["source_evidence"] == 0
    assert result["conflicts"] == [{
        "kind": "unknown_source_evidence_package", "source": "nlp",
        "package_id": "mpkg_missing",
        "reason": "vera.nlp-package-observation/v1",
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


def test_external_conflict_cannot_overwrite_inventory_classification():
    result = project_model_inventory(external_inventories=(("nlp", {
        "schema": "vera.nlp-model-inventory/v1",
        "packages": [], "candidates": [],
        "conflicts": [{"kind": "spoofed", "source": "other", "reason": "collision"}],
    }),))
    assert result["conflicts"] == [{
        "kind": "external_conflict", "source": "nlp", "reason": "collision"}]


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


def test_provider_inventory_is_derived_from_persisted_deployment_health(tmp_path):
    package = _package()
    admission = evaluate_model_admission(
        package, ModelTrustPolicy("inventory"), ModelDeploymentTarget(
            "cpu-a", "ner", "vera.nlp.text/v1", "vera.nlp.ner/v1",
            1_000_000, accelerators=("cpu",),
            frameworks=("onnxruntime",)))
    deployment = define_inference_deployment(
        package, admission, provider_id="onnx:cpu-a",
        runtime_kind="onnxruntime", runtime_version="1",
        placements=("cpu", "local"), retry_owner="nlp-router")
    store = SQLiteInferenceDeploymentRegistry(tmp_path / "deployments.sqlite")
    store.register(deployment)
    health = InferenceProviderHealth(
        "onnx:cpu-a", "ready", 100, 1_000, "runtime-probe",
        (package.package_id,), concurrency_limit=2, revision=3)
    observation = store.observe(
        deployment.deployment_id, expected_revision=0,
        desired_state="active", health=health)

    providers = project_persisted_inference_providers(
        store.list(), (observation,))
    assert len(providers) == 1
    assert providers[0]["provider_id"] == "onnx:cpu-a"
    assert providers[0]["package_ids"] == [package.package_id]
    assert providers[0]["placements"] == ["cpu", "local"]
    assert providers[0]["health"] == health.to_dict()

    inventory = project_model_inventory(
        packages=(package,), deployments=store.list(),
        observations=(observation,), providers=providers)
    assert inventory["counts"]["providers"] == 1
    assert inventory["packages"][0]["providers"][0]["state"] == "ready"


def test_provider_inventory_rejects_orphaned_health_evidence():
    with pytest.raises(ValueError, match="no deployment"):
        project_persisted_inference_providers((), ({
            "deployment_id": "missing",
            "health": {
                "provider_id": "onnx:cpu-a", "observed_at_ms": 1,
                "revision": 1, "evidence_id": "evidence",
            },
        },))
