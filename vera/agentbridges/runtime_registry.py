"""Static registry of shipped RuntimeAdapter instances.

Importing this module declares adapters only. It does not import optional
runtime libraries, inspect Docker, build images, contact models, or execute.
"""
from __future__ import annotations

import os

from Vera.vera.agentbridges.runtime_adapter import ContainerRuntimeAdapter
from Vera.vera.langgraph.runtime_contract import langgraph_runtime_descriptor


RUNTIME_ADAPTERS = {
    "langgraph": ContainerRuntimeAdapter(langgraph_runtime_descriptor(
        os.environ.get("LANGGRAPH_IMAGE", "vera-langgraph:latest"))),
}

