"""Static LangGraph RuntimeAdapter declaration; imports no optional runtime."""
from __future__ import annotations

from Vera.vera.agentbridges.runtime_adapter import (
    RuntimeAdapterDescriptor, feature_set,
)


def langgraph_runtime_descriptor(image: str = "vera-langgraph:latest") -> RuntimeAdapterDescriptor:
    return RuntimeAdapterDescriptor(
        runtime_id="langgraph",
        label="LangGraph isolated container bridge",
        event_prefix="langgraph.run",
        image=image,
        package_refs=("langgraph==1.2.11",),
        features=feature_set({
            "acquisition": ("supported", "Pinned isolated image build is explicit and separate from execution."),
            "health": ("supported", "Docker and image availability have a non-mutating health path."),
            "dependency_isolation": ("supported", "Every run uses the dedicated throwaway LangGraph image."),
            "run": ("supported", "Validated container requests delegate to the shared bridge runner."),
            "stream": ("supported", "BRIDGE_STEP and BRIDGE_RESULT lines stream through the shared parser."),
            "events": ("supported", "The existing langgraph.run event prefix is preserved."),
            "cancellation": ("supported", "Validated active run IDs map to exact owned process handles and one terminal event."),
            "resource_gates": ("supported", "The shared cross-process Ollama gate is acquired and always released."),
            "teardown": ("supported", "The runner kills stalled work and Docker removes the throwaway container."),
            "version_reporting": ("partial", "The pinned package and image are declared; live package attestation is queued."),
        }),
    )
