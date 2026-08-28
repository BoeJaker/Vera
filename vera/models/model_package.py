"""Provider-neutral immutable ModelPackage identity and registration."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Mapping, Sequence


MODEL_PACKAGE_SCHEMA = "vera.model-package/v1"
_SHA256 = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")
_IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}\Z")
_FORMATS = {"onnx", "safetensors", "gguf", "pytorch", "tensorflow", "tflite", "openvino"}


def _identifier(value: Any, field_name: str) -> str:
    value = str(value or "").strip()
    if not _IDENT.fullmatch(value):
        raise ValueError(f"{field_name} must be a bounded identifier")
    return value


def _string(value: Any, field_name: str, *, required: bool = False, limit: int = 2048) -> str:
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(f"{field_name} must be a bounded string")
    return value.strip()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def _hash(value: Any) -> str:
    return "mpkg_" + hashlib.sha256(_canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class ModelArtifact:
    role: str
    uri: str
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", _identifier(self.role, "artifact role"))
        object.__setattr__(self, "uri", _string(self.uri, "artifact uri", required=True, limit=4096))
        digest = str(self.sha256 or "").lower()
        if not _SHA256.fullmatch(digest):
            raise ValueError("artifact sha256 must be a canonical digest")
        object.__setattr__(self, "sha256", digest.removeprefix("sha256:"))
        if isinstance(self.size_bytes, bool) or int(self.size_bytes) < 0:
            raise ValueError("artifact size_bytes must be non-negative")
        object.__setattr__(self, "size_bytes", int(self.size_bytes))

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass(frozen=True)
class ModelCompatibility:
    tasks: tuple[str, ...]
    input_contract: str
    output_contract: str
    min_memory_bytes: int = 0
    accelerators: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        tasks = tuple(sorted({_identifier(v, "task") for v in self.tasks}))
        if not tasks:
            raise ValueError("at least one task is required")
        object.__setattr__(self, "tasks", tasks)
        object.__setattr__(self, "input_contract", _identifier(self.input_contract, "input contract"))
        object.__setattr__(self, "output_contract", _identifier(self.output_contract, "output contract"))
        if isinstance(self.min_memory_bytes, bool) or int(self.min_memory_bytes) < 0:
            raise ValueError("min_memory_bytes must be non-negative")
        object.__setattr__(self, "min_memory_bytes", int(self.min_memory_bytes))
        object.__setattr__(self, "accelerators", tuple(sorted({
            _identifier(v, "accelerator") for v in self.accelerators})))

    def to_dict(self) -> dict:
        return {**self.__dict__, "tasks": list(self.tasks),
                "accelerators": list(self.accelerators)}


@dataclass(frozen=True)
class ModelPackage:
    name: str
    version: str
    architecture: str
    format: str
    artifacts: tuple[ModelArtifact, ...]
    compatibility: ModelCompatibility
    framework: str = ""
    framework_version: str = ""
    opset: int | None = None
    tokenizer: str = ""
    preprocessing: str = ""
    source_uri: str = ""
    source_revision: str = ""
    license: str = ""
    signature: str = ""
    training_run_id: str = ""
    evaluation_report_ids: tuple[str, ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()
    package_id: str = field(init=False)
    schema: str = MODEL_PACKAGE_SCHEMA

    def __post_init__(self) -> None:
        for key in ("name", "version", "architecture"):
            object.__setattr__(self, key, _identifier(getattr(self, key), key))
        fmt = str(self.format or "").lower()
        if fmt not in _FORMATS:
            raise ValueError("unsupported model format")
        object.__setattr__(self, "format", fmt)
        artifacts = tuple(self.artifacts)
        if not artifacts or len(artifacts) > 64 or not all(isinstance(v, ModelArtifact) for v in artifacts):
            raise ValueError("artifacts must contain 1..64 ModelArtifact values")
        if len({v.role for v in artifacts}) != len(artifacts):
            raise ValueError("artifact roles must be unique")
        object.__setattr__(self, "artifacts", tuple(sorted(artifacts, key=lambda v: v.role)))
        if not isinstance(self.compatibility, ModelCompatibility):
            raise TypeError("compatibility must be ModelCompatibility")
        for key in ("framework", "framework_version", "tokenizer", "preprocessing",
                    "source_uri", "source_revision", "license", "signature",
                    "training_run_id"):
            object.__setattr__(self, key, _string(getattr(self, key), key, limit=4096))
        if self.opset is not None and (isinstance(self.opset, bool) or not 1 <= int(self.opset) <= 1_000_000):
            raise ValueError("opset must be a positive bounded integer")
        if self.opset is not None:
            object.__setattr__(self, "opset", int(self.opset))
        evaluations = tuple(sorted({_identifier(v, "evaluation report ID")
                                    for v in self.evaluation_report_ids}))
        object.__setattr__(self, "evaluation_report_ids", evaluations)
        metadata = tuple(sorted((_identifier(k, "metadata key"),
                                 _string(v, "metadata value", limit=2048))
                                for k, v in self.metadata))
        if len(metadata) > 128 or len({k for k, _ in metadata}) != len(metadata):
            raise ValueError("metadata keys must be unique and bounded")
        object.__setattr__(self, "metadata", metadata)
        object.__setattr__(self, "package_id", _hash(self.identity_dict()))

    def identity_dict(self) -> dict:
        return {"schema": self.schema, "name": self.name, "version": self.version,
                "architecture": self.architecture, "format": self.format,
                "artifacts": [v.to_dict() for v in self.artifacts],
                "compatibility": self.compatibility.to_dict(), "framework": self.framework,
                "framework_version": self.framework_version, "opset": self.opset,
                "tokenizer": self.tokenizer, "preprocessing": self.preprocessing,
                "source_uri": self.source_uri, "source_revision": self.source_revision,
                "license": self.license, "signature": self.signature,
                "training_run_id": self.training_run_id,
                "evaluation_report_ids": list(self.evaluation_report_ids),
                "metadata": dict(self.metadata)}

    def to_dict(self) -> dict:
        return {"package_id": self.package_id, **self.identity_dict()}


class ModelPackageConflict(ValueError):
    pass


def model_package_from_dict(value: Mapping[str, Any]) -> ModelPackage:
    """Strictly reconstruct a package and verify its content-derived identity."""
    if not isinstance(value, Mapping):
        raise TypeError("model package value must be a mapping")
    expected_id = str(value.get("package_id") or "")
    if value.get("schema", MODEL_PACKAGE_SCHEMA) != MODEL_PACKAGE_SCHEMA:
        raise ValueError("unsupported model package schema")
    try:
        artifacts = tuple(ModelArtifact(**item) for item in value["artifacts"])
        compatibility = ModelCompatibility(**value["compatibility"])
        metadata = tuple(sorted(dict(value.get("metadata") or {}).items()))
        package = ModelPackage(
            name=value["name"], version=value["version"],
            architecture=value["architecture"], format=value["format"],
            artifacts=artifacts, compatibility=compatibility,
            framework=value.get("framework", ""),
            framework_version=value.get("framework_version", ""),
            opset=value.get("opset"), tokenizer=value.get("tokenizer", ""),
            preprocessing=value.get("preprocessing", ""),
            source_uri=value.get("source_uri", ""),
            source_revision=value.get("source_revision", ""),
            license=value.get("license", ""), signature=value.get("signature", ""),
            training_run_id=value.get("training_run_id", ""),
            evaluation_report_ids=tuple(value.get("evaluation_report_ids") or ()),
            metadata=metadata)
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed model package value") from exc
    if expected_id and expected_id != package.package_id:
        raise ModelPackageConflict("stored package identity does not match content")
    return package


class InMemoryModelPackageRegistry:
    def __init__(self) -> None:
        self._packages: dict[str, ModelPackage] = {}
        self._aliases: dict[str, str] = {}

    def register(self, package: ModelPackage) -> ModelPackage:
        if not isinstance(package, ModelPackage):
            raise TypeError("package must be ModelPackage")
        current = self._packages.get(package.package_id)
        if current is not None and current != package:
            raise ModelPackageConflict("package identity collision")
        self._packages[package.package_id] = package
        return package

    def alias(self, name: str, package_id: str, *, expected_package_id: str = "") -> None:
        name = _identifier(name, "alias")
        if package_id not in self._packages:
            raise KeyError("package is not registered")
        current = self._aliases.get(name, "")
        if current != expected_package_id:
            raise ModelPackageConflict("alias compare-and-set failed")
        self._aliases[name] = package_id

    def get(self, identity: str) -> ModelPackage | None:
        package_id = self._aliases.get(identity, identity)
        return self._packages.get(package_id)

    def list(self) -> tuple[ModelPackage, ...]:
        return tuple(self._packages[key] for key in sorted(self._packages))

    def aliases(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._aliases.items()))
