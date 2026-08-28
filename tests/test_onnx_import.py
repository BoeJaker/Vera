from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from vera.models.model_package import (
    InMemoryModelPackageRegistry, ModelArtifact, ModelCompatibility, ModelPackage)
from vera.models.onnx_import import inspect_and_register_onnx


pytestmark = pytest.mark.critical


def package_for(path: Path, *, fmt: str = "onnx", role: str = "model",
                digest: str | None = None, size: int | None = None) -> ModelPackage:
    data = path.read_bytes() if path.exists() else b""
    artifact = ModelArtifact(
        role=role, uri=str(path), sha256=digest or hashlib.sha256(data).hexdigest(),
        size_bytes=len(data) if size is None else size)
    return ModelPackage(
        name="fixture", version="1", architecture="fixture", format=fmt,
        artifacts=(artifact,),
        compatibility=ModelCompatibility(
            tasks=("classification",), input_contract="tensor/v1",
            output_contract="scores/v1"), framework="onnx", opset=18)


def test_import_registers_without_mutating_or_loading_source(tmp_path: Path) -> None:
    source = tmp_path / "model.onnx"
    source.write_bytes(b"not executed; deterministic fixture")
    before = (source.read_bytes(), source.stat().st_mtime_ns)
    registry = InMemoryModelPackageRegistry()

    receipt = inspect_and_register_onnx(package_for(source), registry)

    assert receipt.registered
    assert len(receipt.verifications) == 1
    assert receipt.verifications[0].verified
    assert registry.get(receipt.package_id) is not None
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before


@pytest.mark.parametrize("change", ["missing", "size", "hash", "cancelled", "limit"])
def test_failed_verification_never_partially_registers(tmp_path: Path, change: str) -> None:
    source = tmp_path / "model.onnx"
    source.write_bytes(b"fixture")
    package = package_for(
        source,
        size=999 if change == "size" else None,
        digest="0" * 64 if change == "hash" else None)
    if change == "missing":
        source.unlink()
    registry = InMemoryModelPackageRegistry()
    kwargs = {}
    if change == "cancelled":
        kwargs["cancelled"] = lambda: True
    if change == "limit":
        kwargs["max_bytes"] = 1

    receipt = inspect_and_register_onnx(package, registry, **kwargs)

    assert receipt.status == "verification_failed"
    assert registry.list() == ()


def test_rejects_non_onnx_package_before_registry_write(tmp_path: Path) -> None:
    source = tmp_path / "model.onnx"
    source.write_bytes(b"fixture")
    registry = InMemoryModelPackageRegistry()
    with pytest.raises(ValueError, match="onnx ModelPackage"):
        inspect_and_register_onnx(package_for(source, fmt="pytorch"), registry)
    assert registry.list() == ()


def test_rejects_wrong_extension_or_missing_model_role(tmp_path: Path) -> None:
    source = tmp_path / "model.bin"
    source.write_bytes(b"fixture")
    registry = InMemoryModelPackageRegistry()
    with pytest.raises(ValueError, match="extension"):
        inspect_and_register_onnx(package_for(source), registry)
    with pytest.raises(ValueError, match="exactly one model"):
        inspect_and_register_onnx(package_for(source, role="weights"), registry)
    assert registry.list() == ()


def test_cancellation_precedes_even_empty_file_verification(tmp_path: Path) -> None:
    source = tmp_path / "empty.onnx"
    source.write_bytes(b"")
    registry = InMemoryModelPackageRegistry()

    receipt = inspect_and_register_onnx(
        package_for(source), registry, cancelled=lambda: True)

    assert receipt.verifications[0].status == "cancelled"
    assert registry.list() == ()
