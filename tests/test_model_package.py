import pytest

from vera.models.model_package import (
    InMemoryModelPackageRegistry, ModelArtifact, ModelCompatibility,
    ModelPackage, ModelPackageConflict)


pytestmark = pytest.mark.critical


def package(**overrides):
    values = dict(
        name="encoder", version="1.0.0", architecture="transformer.encoder",
        format="onnx", artifacts=(ModelArtifact("weights", "artifact://sha256/a",
                                                  "a" * 64, 123),),
        compatibility=ModelCompatibility(("embeddings",), "tokens/v1", "vector/v1",
                                         min_memory_bytes=1024, accelerators=("cpu",)),
        framework="onnxruntime", framework_version="1.22", opset=17,
        tokenizer="tokenizer.pkg/v1", preprocessing="utf8-nfc/v1",
        source_uri="https://example.invalid/model", source_revision="revision-1",
        license="apache-2.0", evaluation_report_ids=("eval-2", "eval-1"))
    values.update(overrides)
    return ModelPackage(**values)


def test_identity_is_canonical_and_serializable():
    first = package()
    second = package(evaluation_report_ids=("eval-1", "eval-2"))
    assert first.package_id == second.package_id
    assert first.to_dict()["artifacts"][0]["sha256"] == "a" * 64
    assert first.to_dict()["compatibility"]["tasks"] == ["embeddings"]


def test_artifacts_are_identity_bound_and_roles_are_unique():
    assert package(artifacts=(ModelArtifact("weights", "artifact://a", "b" * 64, 1),)).package_id != package().package_id
    duplicate = ModelArtifact("weights", "artifact://b", "b" * 64, 1)
    with pytest.raises(ValueError, match="roles"):
        package(artifacts=(package().artifacts[0], duplicate))


@pytest.mark.parametrize("digest,size", [("bad", 1), ("a" * 64, -1)])
def test_invalid_artifact_evidence_fails_closed(digest, size):
    with pytest.raises(ValueError):
        ModelArtifact("weights", "artifact://a", digest, size)


def test_registry_registration_is_idempotent_and_aliases_are_compare_and_set():
    registry = InMemoryModelPackageRegistry()
    value = package()
    assert registry.register(value) is value
    assert registry.register(value) is value
    registry.alias("default-encoder", value.package_id)
    assert registry.get("default-encoder") == value
    with pytest.raises(ModelPackageConflict, match="compare-and-set"):
        registry.alias("default-encoder", value.package_id)


def test_alias_cannot_reference_unregistered_package():
    with pytest.raises(KeyError):
        InMemoryModelPackageRegistry().alias("default", "mpkg_missing")


def test_contract_rejects_unsupported_format_and_bad_compatibility():
    with pytest.raises(ValueError, match="format"):
        package(format="pickle")
    with pytest.raises(ValueError, match="task"):
        ModelCompatibility((), "input/v1", "output/v1")


def test_registry_does_not_open_or_rewrite_artifact_uris(tmp_path):
    target = tmp_path / "model.onnx"
    target.write_bytes(b"untouched")
    value = package(artifacts=(ModelArtifact("weights", str(target), "c" * 64, 9),))
    InMemoryModelPackageRegistry().register(value)
    assert target.read_bytes() == b"untouched"
