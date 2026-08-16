"""
agentbridge_registry.py — declarative catalog of external agent-loop bridges
==============================================================================
The metadata half of every bridge in vera/<name>/<name>_capabilities.py —
paradigm, pinned pip package versions, image name, Dockerfile location, the
env var that opts it in, its `<name>.run` capability. Static Python data, not
persisted (unlike vera/mcp/mcp_catalog_capabilities.py's Redis-backed
catalog — there's no per-operator config here, every bridge ships with
Vera; only the ENABLED flag is runtime state, and that's already owned by
each bridge's own `SMOLAGENTS_ENABLED`-style env var, not duplicated here).

Adding a new bridge = one entry here + the bridge's own module (Dockerfile,
entrypoint speaking the BRIDGE_STEP:/BRIDGE_RESULT: protocol — see
agentbridge_runtime.py — and a thin capabilities.py that calls
stream_bridge_container). agentbridge_capabilities.py's catalog/status/
check_updates caps and the catalog panel all iterate this list generically;
none of them need touching for a new entry to show up.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass(frozen=True)
class BridgeSpec:
    id: str                     # matches the capability namespace, e.g. "smolagents"
    label: str
    icon: str
    paradigm: str                # one-line "how it takes actions" summary
    description: str
    docs_url: str
    pip_packages: Dict[str, str]  # {package: pinned version} — the source of
                                  # truth a human bumps; check_updates compares
                                  # this against PyPI, never writes to it.
    image: str
    dockerfile_rel: str          # relative to vera/<id>/
    enabled_env: str
    run_cap: str                 # e.g. "smolagents.run"
    status_cap: str              # e.g. "smolagents.status"
    event_type_prefix: str       # e.g. "smolagents.run"


BRIDGES: List[BridgeSpec] = [
    BridgeSpec(
        id="smolagents",
        label="smolagents",
        icon="🐣",
        paradigm="Code-as-action — writes and executes Python as its tool-call mechanism",
        description="Hugging Face's minimal (~1,000 LOC core) ReAct agent. "
                    "Distinctive: actions ARE Python code the agent writes and "
                    "runs, not JSON tool-calls.",
        docs_url="https://github.com/huggingface/smolagents",
        pip_packages={"smolagents": "1.26.0"},
        image="vera-smolagents:latest",
        dockerfile_rel="Dockerfile.smolagents",
        enabled_env="SMOLAGENTS_ENABLED",
        run_cap="smolagents.run",
        status_cap="smolagents.status",
        event_type_prefix="smolagents.run",
    ),
    BridgeSpec(
        id="langgraph",
        label="LangGraph",
        icon="🕸️",
        paradigm="Explicit graph of nodes/edges with real structured (JSON) tool-calling",
        description="langchain-ai's graph-based agent orchestration — the "
                    "industry-standard baseline. Opposite paradigm from "
                    "smolagents: a compiled state graph, not code execution.",
        docs_url="https://github.com/langchain-ai/langgraph",
        pip_packages={
            "langgraph": "1.2.11",
            "langgraph-prebuilt": "1.1.0",
            "langchain-openai": "1.5.1",
            "langchain-core": "1.5.5",
        },
        image="vera-langgraph:latest",
        dockerfile_rel="Dockerfile.langgraph",
        enabled_env="LANGGRAPH_ENABLED",
        run_cap="langgraph.run",
        status_cap="langgraph.status",
        event_type_prefix="langgraph.run",
    ),
    BridgeSpec(
        id="pydanticai",
        label="PydanticAI",
        icon="🧬",
        paradigm="Typed, schema-first — output validated against a Pydantic model, graph nodes async-iterated",
        description="Pydantic's agent framework: strongly-typed structured "
                    "output (not free-text), tool calls resolved through the "
                    "same validation path. A third distinct paradigm from "
                    "smolagents (code) and LangGraph (message graph).",
        docs_url="https://github.com/pydantic/pydantic-ai",
        pip_packages={"pydantic-ai-slim": "2.31.0"},
        image="vera-pydanticai:latest",
        dockerfile_rel="Dockerfile.pydanticai",
        enabled_env="PYDANTICAI_ENABLED",
        run_cap="pydanticai.run",
        status_cap="pydanticai.status",
        event_type_prefix="pydanticai.run",
    ),
]

BY_ID: Dict[str, BridgeSpec] = {b.id: b for b in BRIDGES}
