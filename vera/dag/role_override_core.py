"""Per-run model choices for the agentic loop's roles, and effort presets
(user, 2026-09-28): the executor configurable to an MoE such as
Qwen-AgentWorld-35B-A3B; qwen3-coder:30b as the coder for code-intensive
loops, even as a one-click "bigger coder"; and a MAX EFFORT mode that uses the
CPU nodes' larger models as much as it can.

Why a run-level override and not the routing page: the Model Routing page
changes a role for every run on the box; these are choices for ONE run. And a
run's single `model` applies to every role and every role prefers the GPU - an
MoE (AgentWorld 21.7 GB, qwen3-coder:30b 18.6 GB, qwen3.6:35b 23.9 GB) does not
fit the 12 GB V100, so pointed at one the GPU would half-load it and evict its
own model. So each override carries a NODE, and `auto` sends a model that does
not fit the GPU to a CPU node.

Compute roles still hold: a CPU role is slow (measured MoE decode ~10 tok/s vs
~20 on the GPU); the presets place the big models on DIFFERENT CPU nodes (cpu-247
holds the long-horizon 35B; 50 GB RAM cannot also hold AgentWorld).

Pure: names and sizes in, decisions out. capability_orchestration applies them
inside ollama_generate for the run's own calls (a run-scoped ContextVar).
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

PROFILE = "loop"
ROLES = ("executor", "coder")
NODES = ("auto", "gpu", "cpu-247", "cpu-246")
#: Where `auto` sends a model that does not fit the GPU.
AUTO_CPU_NODE = "cpu-247"
#: A model counts as fitting the GPU when its weights take at most this share of
#: usable VRAM - the rest is the KV window.
GPU_FIT_SHARE = 0.85

BIG_CODER = "qwen3-coder:30b"
#: Offered in the loop settings (installed on every node - the shared store).
EXECUTOR_MODELS = ("hf.co/unsloth/Qwen-AgentWorld-35B-A3B-GGUF:MXFP4_MOE",
                   "qwen3.6:35b-a3b", "qwen3-coder:30b")

EFFORTS = ("standard", "bigger-coder", "max")
#: Styles max effort upgrades to stepwise-reviewed (the per-step CPU critic);
#: broad / broad-stepwise / detailed already use the CPU node and keep theirs.
MAX_UPGRADES_STYLE = ("auto", "flat", "stepwise")


def key(role: str, profile: str = PROFILE) -> str:
    return "%s/%s" % (profile, role)


def normalise_node(node: Any) -> str:
    n = str(node or "auto").strip().lower()
    return n if n in NODES else "auto"


def build(*, executor_model: str = "", executor_node: str = "auto",
          coder_model: str = "", coder_node: str = "auto", effort: str = "standard",
          plan_style: str = "auto") -> Tuple[Dict[str, Dict[str, str]], Dict[str, Any]]:
    """({'loop/executor': {model, node}, ...}, adjustments) for one run.

    adjustments may carry `plan_style` (max effort upgrades a stepwise-family
    style to stepwise-reviewed) and `critic_route` ('cpu'). An explicit
    executor/coder model always wins over the preset's."""
    eff = str(effort or "standard").strip().lower()
    eff = eff if eff in EFFORTS else "standard"
    out: Dict[str, Dict[str, str]] = {}
    adj: Dict[str, Any] = {"effort": eff}
    coder = (coder_model or "").strip()
    cnode = normalise_node(coder_node)
    if not coder and eff in ("bigger-coder", "max"):
        coder = BIG_CODER
        # max effort keeps cpu-247 for the long-horizon 35B (critic / briefs):
        # the big coder goes to the other CPU node.
        if eff == "max" and cnode == "auto":
            cnode = "cpu-246"
    if coder:
        out[key("coder")] = {"model": coder, "node": cnode}
    ex = (executor_model or "").strip()
    if ex:
        out[key("executor")] = {"model": ex, "node": normalise_node(executor_node)}
    if eff == "max":
        style = str(plan_style or "auto").strip().lower()
        if style in MAX_UPGRADES_STYLE:
            adj["plan_style"] = "stepwise-reviewed"
        adj["critic_route"] = "cpu"
    return out, adj


def resolve_node(node: str, *, model_bytes: int = 0, gpu_usable_bytes: int = 0) -> str:
    """'gpu' or a CPU node id for an override's node. `auto` = the GPU when the
    model's weights fit it (GPU_FIT_SHARE of usable VRAM), else AUTO_CPU_NODE;
    an unknown size is never gambled on the GPU (a half-loaded model there
    evicts the GPU's own model)."""
    n = normalise_node(node)
    if n != "auto":
        return n
    if model_bytes > 0 and gpu_usable_bytes > 0 and model_bytes <= gpu_usable_bytes * GPU_FIT_SHARE:
        return "gpu"
    return AUTO_CPU_NODE


def describe(overrides: Dict[str, Dict[str, str]], adj: Dict[str, Any]) -> str:
    parts = ["%s=%s@%s" % (k.split("/", 1)[1], v.get("model"), v.get("node"))
             for k, v in sorted(overrides.items())]
    if adj.get("plan_style"):
        parts.append("style->%s" % adj["plan_style"])
    return "effort %s%s" % (adj.get("effort", "standard"), (": " + ", ".join(parts)) if parts else "")
