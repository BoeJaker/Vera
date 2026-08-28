"""Inspect-only ONNX artifact registration.

This boundary deliberately does not import onnx/onnxruntime, construct a model
session, or take ownership of the source file.  It verifies declared bytes and
registers the immutable package only when every artifact is intact.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable, Protocol
from urllib.parse import unquote, urlparse

from .model_package import ModelArtifact, ModelPackage
from .model_package_store import (
    MAX_VERIFY_BYTES, ArtifactVerificationReceipt, verify_local_artifact)


class ModelPackageRegistrar(Protocol):
    def register(self, package: ModelPackage) -> ModelPackage: ...


@dataclass(frozen=True)
class ONNXImportReceipt:
    package_id: str
    status: str
    verifications: tuple[ArtifactVerificationReceipt, ...]

    @property
    def registered(self) -> bool:
        return self.status == "registered"

    def to_dict(self) -> dict:
        return {"package_id": self.package_id, "status": self.status,
                "registered": self.registered,
                "verifications": [item.to_dict() for item in self.verifications]}


def _uri_suffix(artifact: ModelArtifact) -> str:
    parsed = urlparse(artifact.uri)
    path = unquote(parsed.path) if parsed.scheme == "file" else artifact.uri
    return PurePosixPath(path.replace("\\", "/")).suffix.lower()


def inspect_and_register_onnx(
        package: ModelPackage, registry: ModelPackageRegistrar, *,
        max_bytes: int = MAX_VERIFY_BYTES,
        cancelled: Callable[[], bool] | None = None) -> ONNXImportReceipt:
    """Verify an ONNX package by reference, then register it atomically.

    Registration happens only after every declared artifact verifies.  The
    source files are never renamed, moved, deleted, parsed, loaded or executed.
    """
    if not isinstance(package, ModelPackage):
        raise TypeError("package must be ModelPackage")
    if package.format != "onnx":
        raise ValueError("ONNX import requires an onnx ModelPackage")
    model_artifacts = tuple(item for item in package.artifacts if item.role == "model")
    if len(model_artifacts) != 1:
        raise ValueError("ONNX import requires exactly one model artifact")
    if _uri_suffix(model_artifacts[0]) != ".onnx":
        raise ValueError("ONNX model artifact must use the .onnx extension")

    receipts: list[ArtifactVerificationReceipt] = []
    for artifact in package.artifacts:
        receipt = verify_local_artifact(
            package.package_id, artifact, max_bytes=max_bytes, cancelled=cancelled)
        receipts.append(receipt)
        if not receipt.verified:
            return ONNXImportReceipt(package.package_id, "verification_failed",
                                     tuple(receipts))
    registry.register(package)
    return ONNXImportReceipt(package.package_id, "registered", tuple(receipts))
