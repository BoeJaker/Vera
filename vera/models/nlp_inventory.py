"""Deterministic projection of deployed NLP models into ModelPackage inventory.

Node discovery is allowed to report a model name and presence without claiming
that it is a portable package.  A verified package requires the content hashes
and compatibility contract carried by ``ModelPackage``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from .model_package import (ModelArtifact, ModelCompatibility, ModelPackage,
                            ModelPackageConflict, model_package_from_dict)


def package_nlp_directory(*, task: str, model: str, kind: str,
                          directory: str | Path,
                          framework_version: str = "") -> ModelPackage:
    """Create a content-addressed package for an already exported directory.

    This is an export-time operation, never a request-time operation.  It reads
    files but neither loads the model nor contacts a registry.
    """
    root = Path(directory).resolve()
    files = sorted(path for path in root.rglob("*") if path.is_file())
    if not files or len(files) > 64:
        raise ValueError("NLP package directory must contain 1..64 files")
    if not any(path.suffix.lower() == ".onnx" for path in files):
        raise ValueError("NLP package directory must contain an ONNX artifact")
    artifacts = []
    content = hashlib.sha256()
    for index, path in enumerate(files, 1):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        hexdigest = digest.hexdigest()
        content.update(f"{relative}\0{hexdigest}\0{size}\n".encode())
        artifacts.append(ModelArtifact(
            role=f"artifact-{index:03d}",
            uri=f"nlp-store://{root.name}/{relative}",
            sha256=hexdigest, size_bytes=size))

    architecture = kind or "unknown"
    config_path = root / "config.json"
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            values = config.get("architectures") or []
            if values:
                architecture = str(values[0])
        except (OSError, ValueError, TypeError):
            pass
    architecture = re.sub(r"[^A-Za-z0-9._:/+-]", "-", architecture)[:256]
    name = re.sub(r"[^A-Za-z0-9._:/+-]", "-", model)[:256]
    version = "content-" + content.hexdigest()[:16]
    return ModelPackage(
        name=name, version=version, architecture=architecture, format="onnx",
        artifacts=tuple(artifacts),
        compatibility=ModelCompatibility(
            tasks=(task,), input_contract="vera.nlp.text/v1",
            output_contract=f"vera.nlp.{task}/v1", accelerators=("cpu",)),
        framework="onnxruntime", framework_version=framework_version,
        tokenizer=model, source_uri=f"hf://{model}",
        metadata=(("deployment", "edge-nlp"), ("model_id", model)))


def project_nlp_inventory(nodes: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Return verified packages and honest candidates from NLP node facts.

    The function is pure and deliberately does not register packages.  A node
    may expose a strict ``model_package`` beside a task; malformed packages are
    downgraded to candidates rather than admitted under an invented identity.
    """
    packages: dict[str, dict[str, Any]] = {}
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    conflicts: list[dict[str, str]] = []

    for node in sorted(nodes, key=lambda value: str(value.get("node_id") or "")):
        node_id = str(node.get("node_id") or "")
        tasks = node.get("tasks") or {}
        if not isinstance(tasks, Mapping):
            continue
        for task, raw in sorted(tasks.items(), key=lambda item: str(item[0])):
            task = str(task or "").strip()
            row = raw if isinstance(raw, Mapping) else {}
            model = str(row.get("model") or "").strip()
            key = (task, model)
            package_value = row.get("model_package")
            blocker = ""
            if package_value:
                try:
                    package = model_package_from_dict(package_value)
                    if not row.get("present"):
                        raise ValueError("package is not present on reporting node")
                    if package.format != "onnx":
                        raise ValueError("NLP deployment package must use ONNX format")
                    if task not in package.compatibility.tasks:
                        raise ValueError("package compatibility does not include deployed task")
                    if dict(package.metadata).get("model_id") != model:
                        raise ValueError("package model identity does not match deployment")
                    current = packages.get(package.package_id)
                    value = package.to_dict()
                    if current is not None and current != value:
                        raise ModelPackageConflict("package identity collision across nodes")
                    packages[package.package_id] = value
                    continue
                except (TypeError, ValueError) as exc:
                    blocker = f"invalid_model_package:{type(exc).__name__}"

            candidate = candidates.setdefault(key, {
                "task": task,
                "model": model,
                "format": "onnx",
                "present_on": [],
                "loaded_on": [],
                "blockers": [],
            })
            if node_id and row.get("present"):
                candidate["present_on"].append(node_id)
            if node_id and row.get("loaded"):
                candidate["loaded_on"].append(node_id)
            reason = blocker or ("model_not_present" if not row.get("present")
                                 else "missing_content_verified_manifest")
            candidate["blockers"].append(reason)

    normalized = []
    for candidate in candidates.values():
        candidate["present_on"] = sorted(set(candidate["present_on"]))
        candidate["loaded_on"] = sorted(set(candidate["loaded_on"]))
        candidate["blockers"] = sorted(set(candidate["blockers"]))
        candidate["status"] = "unresolved"
        normalized.append(candidate)
    normalized.sort(key=lambda value: (value["task"], value["model"]))

    return {
        "schema": "vera.nlp-model-inventory/v1",
        "packages": [packages[key] for key in sorted(packages)],
        "candidates": normalized,
        "conflicts": conflicts,
        "counts": {"packages": len(packages), "candidates": len(normalized)},
    }
