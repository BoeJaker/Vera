"""Offline distributed-training plans with an Accelerate-compatible shape.

This module deliberately imports no training framework and launches nothing.
It turns Vera's authoritative ``TrainingRequest`` into a bounded, immutable
plan that a separately isolated runtime may later resolve and execute.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import re
from typing import Any, Mapping, Sequence

from ..execution.run_protocol import ArtifactRef
from .model_package import _identifier, _string
from .training_contracts import TrainingRequest


DISTRIBUTED_RUNTIME_SCHEMA = "vera.distributed-training-runtime/v1"
ACCELERATE_PLAN_SCHEMA = "vera.accelerate-launch-plan/v1"
MAX_PLAN_BYTES = 256 * 1024
_SHA256 = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")
_MODULE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\Z")
_ENV = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?\Z")
_SECRET = re.compile(
    r"(?:^|_)(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY|CREDENTIALS?)(?:_|$)",
    re.I)
_DEVICES = {"cpu", "cuda", "xpu", "mps"}
_BACKENDS = {"single", "ddp", "fsdp", "deepspeed"}
_PRECISIONS = {"fp32", "fp16", "bf16"}


def _secret_like(value: str) -> bool:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return bool(_SECRET.search(normalized))


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


def _sha256(value: Any, label: str) -> str:
    digest = str(value or "").strip().lower()
    if not _SHA256.fullmatch(digest):
        raise ValueError(f"{label} must be a canonical SHA-256 digest")
    return "sha256:" + digest.removeprefix("sha256:")


def _identifiers(values: Sequence[str], label: str, allowed: set[str],
                 *, maximum: int = 16) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{label} must be a sequence")
    result = tuple(sorted({_identifier(value, label) for value in values}))
    if not result or len(result) > maximum or set(result) - allowed:
        raise ValueError(f"{label} contains unsupported values")
    return result


def _positive_int(value: Any, label: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    result = value
    if not 1 <= result <= maximum:
        raise ValueError(f"{label} must be between 1 and {maximum}")
    return result


def _artifact_projection(value: ArtifactRef, label: str) -> dict[str, Any]:
    if not isinstance(value, ArtifactRef):
        raise TypeError(f"{label} must be an ArtifactRef")
    if value.size_bytes is None or value.size_bytes <= 0:
        raise ValueError(f"{label} must have a positive verified size")
    return {
        "id": _identifier(value.id, f"{label} ID"),
        "kind": _identifier(value.kind, f"{label} kind"),
        "uri_digest": _digest(value.uri),
        "checksum": _sha256(value.checksum, f"{label} checksum"),
        "media_type": _string(value.media_type, f"{label} media type", limit=255),
        "size_bytes": value.size_bytes,
    }


@dataclass(frozen=True)
class DistributedRuntimeProfile:
    runtime_id: str
    adapter_version: str
    framework_version: str
    source_revision: str
    devices: tuple[str, ...]
    backends: tuple[str, ...]
    precisions: tuple[str, ...]
    environment_allowlist: tuple[str, ...] = ()
    maximum_machines: int = 8
    maximum_processes_per_machine: int = 16
    profile_id: str = field(init=False)
    schema: str = DISTRIBUTED_RUNTIME_SCHEMA

    def __post_init__(self) -> None:
        for name in ("runtime_id", "adapter_version", "framework_version"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        if (not _VERSION.fullmatch(self.adapter_version) or
                not _VERSION.fullmatch(self.framework_version)):
            raise ValueError("runtime versions must be pinned release versions")
        object.__setattr__(self, "source_revision", _sha256(
            self.source_revision, "runtime source revision"))
        object.__setattr__(self, "devices", _identifiers(
            self.devices, "device", _DEVICES))
        object.__setattr__(self, "backends", _identifiers(
            self.backends, "backend", _BACKENDS))
        object.__setattr__(self, "precisions", _identifiers(
            self.precisions, "precision", _PRECISIONS))
        allowlist = tuple(sorted({_string(name, "environment name", limit=128)
                                  for name in self.environment_allowlist}))
        if len(allowlist) > 64 or any(
                not _ENV.fullmatch(name) or _secret_like(name)
                for name in allowlist):
            raise ValueError("environment allowlist contains an unsafe name")
        object.__setattr__(self, "environment_allowlist", allowlist)
        object.__setattr__(self, "maximum_machines", _positive_int(
            self.maximum_machines, "maximum machines", 64))
        object.__setattr__(self, "maximum_processes_per_machine", _positive_int(
            self.maximum_processes_per_machine,
            "maximum processes per machine", 64))
        object.__setattr__(self, "profile_id", "dtrp_" + _digest(
            self.identity_dict()).removeprefix("sha256:"))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "runtime_id": self.runtime_id,
            "adapter": "accelerate",
            "adapter_version": self.adapter_version,
            "framework_version": self.framework_version,
            "source_revision": self.source_revision,
            "devices": list(self.devices),
            "backends": list(self.backends),
            "precisions": list(self.precisions),
            "environment_allowlist": list(self.environment_allowlist),
            "maximum_machines": self.maximum_machines,
            "maximum_processes_per_machine": self.maximum_processes_per_machine,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"profile_id": self.profile_id, **self.identity_dict()}


@dataclass(frozen=True)
class DistributedTopology:
    device: str
    backend: str
    precision: str
    machines: int = 1
    processes_per_machine: int = 1
    machine_rank: int = 0
    rendezvous_ref: str = ""
    cpu_threads_per_process: int = 1

    def __post_init__(self) -> None:
        for name, allowed in (("device", _DEVICES), ("backend", _BACKENDS),
                              ("precision", _PRECISIONS)):
            value = _identifier(getattr(self, name), name)
            if value not in allowed:
                raise ValueError(f"unsupported {name}")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "machines", _positive_int(
            self.machines, "machines", 64))
        object.__setattr__(self, "processes_per_machine", _positive_int(
            self.processes_per_machine, "processes per machine", 64))
        object.__setattr__(self, "cpu_threads_per_process", _positive_int(
            self.cpu_threads_per_process, "CPU threads per process", 1024))
        if isinstance(self.machine_rank, bool) or not isinstance(self.machine_rank, int):
            raise ValueError("machine rank must be an integer")
        if not 0 <= self.machine_rank < self.machines:
            raise ValueError("machine rank must identify one configured machine")
        rendezvous = (_identifier(self.rendezvous_ref, "rendezvous reference")
                      if self.rendezvous_ref else "")
        object.__setattr__(self, "rendezvous_ref", rendezvous)
        world = self.machines * self.processes_per_machine
        if self.backend == "single" and (world != 1 or self.machine_rank != 0):
            raise ValueError("single backend requires exactly one process")
        if self.backend != "single" and world < 2:
            raise ValueError("distributed backend requires at least two processes")
        if self.machines > 1 and not rendezvous:
            raise ValueError("multi-machine topology requires a rendezvous reference")
        if self.device == "cpu" and self.precision != "fp32":
            raise ValueError("CPU topology supports fp32 only")
        if self.device == "mps" and (self.backend != "single" or world != 1):
            raise ValueError("MPS topology is single-process only")
        if self.backend in {"fsdp", "deepspeed"} and self.device != "cuda":
            raise ValueError("FSDP and DeepSpeed require the CUDA device class")

    @property
    def world_size(self) -> int:
        return self.machines * self.processes_per_machine

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "world_size": self.world_size}


@dataclass(frozen=True)
class CheckpointContract:
    save_every_steps: int
    keep_last: int = 1
    resume_from: ArtifactRef | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "save_every_steps", _positive_int(
            self.save_every_steps, "checkpoint interval", 1_000_000_000))
        object.__setattr__(self, "keep_last", _positive_int(
            self.keep_last, "checkpoint retention", 100))
        if self.resume_from is not None:
            if self.resume_from.kind != "training.checkpoint":
                raise ValueError("resume artifact must be a training checkpoint")
            _artifact_projection(self.resume_from, "resume artifact")

    def to_dict(self) -> dict[str, Any]:
        return {
            "save_every_steps": self.save_every_steps,
            "keep_last": self.keep_last,
            "resume_from": (_artifact_projection(
                self.resume_from, "resume artifact") if self.resume_from else None),
            "output_artifact_kind": "training.checkpoint",
        }


@dataclass(frozen=True)
class AccelerateLaunchPlan:
    training_request_id: str
    runtime_profile_id: str
    runtime_id: str
    source_revision: str
    entrypoint_module: str
    request_projection_json: str = field(repr=False)
    topology_json: str = field(repr=False)
    checkpoint_json: str = field(repr=False)
    entrypoint_artifact_json: str = field(repr=False)
    environment_json: str = field(repr=False)
    application_arguments: tuple[str, ...] = field(repr=False)
    command: tuple[str, ...]
    plan_id: str = field(init=False)
    schema: str = ACCELERATE_PLAN_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_id", "atplan_" + _digest(
            self.identity_dict()).removeprefix("sha256:"))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "training_request_id": self.training_request_id,
            "runtime_profile_id": self.runtime_profile_id,
            "runtime_id": self.runtime_id,
            "source_revision": self.source_revision,
            "entrypoint_module": self.entrypoint_module,
            "request": json.loads(self.request_projection_json),
            "topology": json.loads(self.topology_json),
            "checkpoint": json.loads(self.checkpoint_json),
            "entrypoint_artifact": json.loads(self.entrypoint_artifact_json),
            "environment": json.loads(self.environment_json),
            "application_arguments": list(self.application_arguments),
            "command": list(self.command),
            "progress_unit": "optimizer_step",
            "cancellation": "cooperative_then_terminate",
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            **self.identity_dict(),
            "effect": "none",
            "runtime_invoked": False,
            "environment_values_retained": False,
            "hardware_allocated": False,
            "activation_authority": False,
        }


def _application_arguments(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("application arguments must be a sequence")
    if len(values) > 64:
        raise ValueError("too many application arguments")
    result = []
    for value in values:
        token = _string(value, "application argument", required=True, limit=512)
        if any(ord(character) < 32 for character in token):
            raise ValueError("application arguments cannot contain control characters")
        if _secret_like(token):
            raise ValueError("secret-like application arguments are not portable")
        result.append(token)
    return tuple(result)


def _environment(profile: DistributedRuntimeProfile,
                 values: Mapping[str, Any]) -> list[dict[str, str]]:
    if not isinstance(values, Mapping) or len(values) > 64:
        raise ValueError("environment must be a bounded mapping")
    allowed = set(profile.environment_allowlist)
    projected = []
    for raw_name, raw_value in values.items():
        name = str(raw_name or "").strip()
        if (not _ENV.fullmatch(name) or _secret_like(name) or
                name not in allowed):
            raise ValueError("environment name is not explicitly allowed")
        if isinstance(raw_value, bool):
            value: Any = raw_value
        elif isinstance(raw_value, int):
            value = raw_value
        elif isinstance(raw_value, float):
            if not math.isfinite(raw_value):
                raise ValueError("environment values must be finite")
            value = raw_value
        elif isinstance(raw_value, str):
            value = _string(raw_value, "environment value", limit=2048)
        else:
            raise TypeError("environment values must be JSON scalars")
        projected.append({"name": name, "value_digest": _digest(value)})
    return sorted(projected, key=lambda item: item["name"])


def plan_accelerate_training(
        *, request: TrainingRequest, profile: DistributedRuntimeProfile,
        topology: DistributedTopology, checkpoint: CheckpointContract,
        entrypoint_module: str, entrypoint_artifact: ArtifactRef,
        environment: Mapping[str, Any] | None = None,
        application_arguments: Sequence[str] = ()) -> AccelerateLaunchPlan:
    """Create an immutable launch plan; never import or invoke Accelerate."""
    if not isinstance(request, TrainingRequest):
        raise TypeError("request must be a TrainingRequest")
    if not isinstance(profile, DistributedRuntimeProfile):
        raise TypeError("profile must be a DistributedRuntimeProfile")
    if not isinstance(topology, DistributedTopology):
        raise TypeError("topology must be a DistributedTopology")
    if not isinstance(checkpoint, CheckpointContract):
        raise TypeError("checkpoint must be a CheckpointContract")
    if topology.device not in profile.devices:
        raise ValueError("runtime profile does not support the requested device")
    if topology.backend not in profile.backends:
        raise ValueError("runtime profile does not support the requested backend")
    if topology.precision not in profile.precisions:
        raise ValueError("runtime profile does not support the requested precision")
    if topology.machines > profile.maximum_machines:
        raise ValueError("topology exceeds runtime machine limit")
    if topology.processes_per_machine > profile.maximum_processes_per_machine:
        raise ValueError("topology exceeds runtime process limit")
    module = str(entrypoint_module or "").strip()
    if not _MODULE.fullmatch(module):
        raise ValueError("entrypoint must be a dotted Python module")
    artifact = _artifact_projection(entrypoint_artifact, "entrypoint artifact")
    if entrypoint_artifact.kind != "training.entrypoint":
        raise ValueError("entrypoint artifact must have training.entrypoint kind")
    if entrypoint_artifact.media_type not in {
            "text/x-python", "application/x-python-code"}:
        raise ValueError("entrypoint artifact must declare Python media type")
    arguments = _application_arguments(application_arguments)
    environment_projection = _environment(profile, environment or {})
    request_projection = {
        "training_request_id": request.training_request_id,
        "dataset_revision_id": request.dataset_revision_id,
        "objective": request.objective,
        "base_model_package_id": request.base_model_package_id,
        "prompt_package_id": request.prompt_package_id,
        "hyperparameters_digest": _digest(dict(request.hyperparameters)),
    }
    command = [
        "accelerate", "launch",
        "--num_machines", str(topology.machines),
        "--num_processes", str(topology.world_size),
        "--machine_rank", str(topology.machine_rank),
        "--mixed_precision", {"fp32": "no", "fp16": "fp16", "bf16": "bf16"}[
            topology.precision],
    ]
    if topology.device == "cpu":
        command.append("--cpu")
    if topology.backend == "fsdp":
        command.append("--use_fsdp")
    elif topology.backend == "deepspeed":
        command.append("--use_deepspeed")
    if topology.machines > 1:
        command.extend(("--main_process_endpoint", "{resolve:" +
                        topology.rendezvous_ref + "}"))
    command.extend(("--module", module, "--", *arguments))
    plan = AccelerateLaunchPlan(
        request.training_request_id, profile.profile_id, profile.runtime_id,
        profile.source_revision, module, _canonical(request_projection),
        _canonical(topology.to_dict()), _canonical(checkpoint.to_dict()),
        _canonical(artifact), _canonical(environment_projection), arguments,
        tuple(command))
    if len(_canonical(plan.to_dict()).encode()) > MAX_PLAN_BYTES:
        raise ValueError("Accelerate launch plan exceeds byte limit")
    return plan
