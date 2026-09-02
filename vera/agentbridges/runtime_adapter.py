"""Runtime-neutral contract over Vera's isolated container agent bridges.

The adapter describes lifecycle support without importing an external runtime
or starting a container.  Execution remains delegated to the established
bridge runner so existing event and capability aliases stay authoritative.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Awaitable, Callable, Mapping, Protocol, runtime_checkable

from . import agentbridge_runtime as bridge


SCHEMA = "vera.runtime-adapter/v1"
FEATURES = (
    "acquisition", "health", "dependency_isolation", "run", "stream",
    "events", "cancellation", "resource_gates", "teardown",
    "version_reporting",
)
FEATURE_STATES = {"supported", "partial", "unsupported"}
_ID = re.compile(r"[a-z][a-z0-9._-]{1,63}\Z")
_EVENT_PREFIX = re.compile(r"[a-z][a-z0-9_.-]{1,127}\Z")
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def _bounded(value: Any, label: str, maximum: int = 256) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum:
        raise ValueError(f"{label} must contain 1..{maximum} characters")
    return text


@dataclass(frozen=True)
class RuntimeFeature:
    name: str
    state: str
    evidence: str

    def __post_init__(self) -> None:
        if self.name not in FEATURES:
            raise ValueError(f"unknown runtime feature: {self.name}")
        if self.state not in FEATURE_STATES:
            raise ValueError(f"invalid runtime feature state: {self.state}")
        object.__setattr__(self, "evidence", _bounded(self.evidence, "feature evidence"))

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "state": self.state, "evidence": self.evidence}


@dataclass(frozen=True)
class RuntimeAdapterDescriptor:
    runtime_id: str
    label: str
    event_prefix: str
    image: str
    package_refs: tuple[str, ...]
    features: tuple[RuntimeFeature, ...]
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        if not _ID.fullmatch(self.runtime_id):
            raise ValueError("runtime_id must be a bounded lowercase identifier")
        object.__setattr__(self, "label", _bounded(self.label, "runtime label", 96))
        if not _EVENT_PREFIX.fullmatch(self.event_prefix):
            raise ValueError("event_prefix must be a bounded dotted identifier")
        object.__setattr__(self, "image", _bounded(self.image, "runtime image", 192))
        refs = tuple(sorted({_bounded(item, "package reference", 128)
                             for item in self.package_refs}))
        object.__setattr__(self, "package_refs", refs)
        features = tuple(sorted(self.features, key=lambda item: item.name))
        if (len(features) != len(FEATURES)
                or {item.name for item in features} != set(FEATURES)):
            raise ValueError("runtime adapter must declare every lifecycle feature")
        object.__setattr__(self, "features", features)

    def to_dict(self) -> dict[str, Any]:
        gaps = [item.name for item in self.features if item.state != "supported"]
        run_state = next(item.state for item in self.features if item.name == "run")
        return {
            "schema": self.schema,
            "runtime_id": self.runtime_id,
            "label": self.label,
            "event_prefix": self.event_prefix,
            "image": self.image,
            "package_refs": list(self.package_refs),
            "features": [item.to_dict() for item in self.features],
            "gaps": gaps,
            "inspection_executes": False,
            "execution_supported": run_state == "supported",
        }


@dataclass(frozen=True)
class ContainerRunRequest:
    run_id: str
    session_id: str
    argv: tuple[str, ...]
    timeout_s: int
    stall_s: int
    progress_kinds: frozenset[str] = frozenset()
    gate_instance_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _bounded(self.run_id, "run_id", 128))
        if not _RUN_ID.fullmatch(self.run_id):
            raise ValueError("run_id contains unsupported characters")
        if len(str(self.session_id or "")) > 256:
            raise ValueError("session_id exceeds 256 characters")
        if (not self.argv or len(self.argv) > 128
                or any(not isinstance(item, str) or not item or len(item) > 131_072
                       for item in self.argv)
                or sum(len(item) for item in self.argv) > 262_144):
            raise ValueError("argv must contain non-empty strings")
        if not 1 <= int(self.stall_s) <= int(self.timeout_s) <= 86_400:
            raise ValueError("timeouts must satisfy 1 <= stall_s <= timeout_s <= 86400")


EmitFn = Callable[[dict[str, Any]], Awaitable[None]]


@runtime_checkable
class RuntimeAdapter(Protocol):
    """Minimal lifecycle contract shared by embedded and container runtimes."""

    descriptor: RuntimeAdapterDescriptor

    def inspect(self) -> dict[str, Any]: ...
    async def health(self) -> dict[str, Any]: ...
    async def image_present(self) -> bool: ...
    async def ensure_image(self, *, dockerfile: str, context_dir: str,
                           force: bool = False) -> dict[str, Any]: ...
    async def run(self, request: ContainerRunRequest, *, emit: EmitFn) -> None: ...
    async def cancel(self, run_id: str) -> dict[str, Any]: ...


class ContainerRuntimeAdapter:
    """Validated facade over the existing isolated bridge runner."""

    def __init__(self, descriptor: RuntimeAdapterDescriptor) -> None:
        self.descriptor = descriptor

    def inspect(self) -> dict[str, Any]:
        """Return declarations only; never inspect Docker, import or execute runtime code."""
        return self.descriptor.to_dict()

    async def health(self) -> dict[str, Any]:
        docker = await bridge.sh(
            ["docker", "version", "--format", "{{.Server.Version}}"], timeout=10)
        docker_ok = bool(docker.get("ok"))
        present = await self.image_present() if docker_ok else False
        return {"runtime_id": self.descriptor.runtime_id, "docker_ok": docker_ok,
                "image": self.descriptor.image, "image_present": present}

    async def image_present(self) -> bool:
        return bool(await bridge.image_present(self.descriptor.image))

    async def ensure_image(self, *, dockerfile: str, context_dir: str,
                           force: bool = False) -> dict[str, Any]:
        if not force and await self.image_present():
            return {"ok": True, "present": True, "action": "none"}
        result = await bridge.build_image(
            self.descriptor.image, dockerfile, context_dir)
        return {**result, "action": "build"}

    async def run(self, request: ContainerRunRequest, *, emit: EmitFn) -> None:
        await bridge.stream_bridge_container(
            run_id=request.run_id,
            session_id=request.session_id,
            argv=list(request.argv),
            event_type_prefix=self.descriptor.event_prefix,
            emit=emit,
            timeout_s=request.timeout_s,
            stall_s=request.stall_s,
            progress_kinds=(set(request.progress_kinds)
                            if request.progress_kinds else None),
            gate_instance_id=request.gate_instance_id,
        )

    async def cancel(self, run_id: str) -> dict[str, Any]:
        """Request cancellation through the runner's exact process registry."""
        run_id = _bounded(run_id, "run_id", 128)
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError("run_id contains unsupported characters")
        result = await bridge.cancel_bridge_run(run_id)
        return {**result, "runtime_id": self.descriptor.runtime_id}


def feature_set(states: Mapping[str, tuple[str, str]]) -> tuple[RuntimeFeature, ...]:
    """Build a complete feature declaration and fail on omissions or extras."""
    if set(states) != set(FEATURES):
        raise ValueError("feature declarations must exactly match RuntimeAdapter features")
    return tuple(RuntimeFeature(name, states[name][0], states[name][1])
                 for name in FEATURES)
