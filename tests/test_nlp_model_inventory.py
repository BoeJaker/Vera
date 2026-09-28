import pytest

from vera.models.model_package import ModelArtifact, ModelCompatibility, ModelPackage
from vera.models.nlp_inventory import package_nlp_directory, project_nlp_inventory


pytestmark = pytest.mark.critical


def _package(task="ner"):
    return ModelPackage(
        name="ontonotes-ner", version="content-abc123", architecture="roberta",
        format="onnx",
        artifacts=(ModelArtifact("model", "nlp-store://ner/model.onnx", "a" * 64, 42),),
        compatibility=ModelCompatibility(
            tasks=(task,), input_contract="vera.nlp.text/v1",
            output_contract=f"vera.nlp.{task}/v1", accelerators=("cpu",)),
        framework="onnxruntime", metadata=(("model_id", "owner/model"),)).to_dict()


def test_present_legacy_deployment_is_visible_but_not_misrepresented_as_package():
    result = project_nlp_inventory([
        {"node_id": "cpu-247", "tasks": {
            "ner": {"model": "owner/model", "present": True, "loaded": False}}},
        {"node_id": "cpu-246", "tasks": {
            "ner": {"model": "owner/model", "present": True, "loaded": True}}},
    ])
    assert result["packages"] == []
    assert result["candidates"] == [{
        "task": "ner", "model": "owner/model", "format": "onnx",
        "present_on": ["cpu-246", "cpu-247"], "loaded_on": ["cpu-246"],
        "blockers": ["missing_content_verified_manifest"], "status": "unresolved"}]


def test_verified_package_is_deduplicated_across_nodes():
    package = _package()
    result = project_nlp_inventory([
        {"node_id": node, "tasks": {"ner": {
            "model": "owner/model", "present": True, "model_package": package}}}
        for node in ("cpu-247", "cpu-246")])
    assert result["packages"] == [package]
    assert result["candidates"] == []
    assert result["counts"] == {"packages": 1, "candidates": 0}


def test_invalid_or_wrong_task_package_fails_closed_to_candidate():
    package = _package("classify")
    result = project_nlp_inventory([{"node_id": "cpu-247", "tasks": {
        "ner": {"model": "owner/model", "present": True,
                "model_package": package}}}])
    assert result["packages"] == []
    assert result["candidates"][0]["blockers"] == ["invalid_model_package:ValueError"]


def test_package_for_another_model_or_absent_artifact_fails_closed():
    package = _package()
    for present, model in ((True, "another/model"), (False, "owner/model")):
        result = project_nlp_inventory([{"node_id": "cpu-247", "tasks": {
            "ner": {"model": model, "present": present,
                    "model_package": package}}}])
        assert result["packages"] == []
        assert result["candidates"][0]["blockers"] == [
            "invalid_model_package:ValueError"]


def test_exported_directory_becomes_content_addressed_package(tmp_path):
    model_dir = tmp_path / "owner__model"
    model_dir.mkdir()
    (model_dir / "model.onnx").write_bytes(b"onnx-content")
    (model_dir / "config.json").write_text(
        '{"architectures":["RobertaForTokenClassification"]}', encoding="utf-8")
    first = package_nlp_directory(
        task="ner", model="owner/model", kind="token-classification",
        directory=model_dir, framework_version="1.2.3")
    second = package_nlp_directory(
        task="ner", model="owner/model", kind="token-classification",
        directory=model_dir, framework_version="1.2.3")
    assert first == second
    assert first.format == "onnx"
    assert first.compatibility.tasks == ("ner",)
    assert {item.uri for item in first.artifacts} == {
        "nlp-store://owner__model/config.json",
        "nlp-store://owner__model/model.onnx"}
    assert all(len(item.sha256) == 64 for item in first.artifacts)


def test_directory_without_onnx_is_not_packaged(tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="ONNX artifact"):
        package_nlp_directory(
            task="ner", model="owner/model", kind="token-classification",
            directory=tmp_path)
