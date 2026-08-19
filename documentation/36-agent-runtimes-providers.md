# 36 · Agent Runtimes, Providers, and Model Catalog

Vera can run its own DAG/loop engine and can also delegate work to external
agent frameworks. The Agent Bridges layer normalizes those runtimes; Providers
manage hosted-model connections and usage; Catalog helps choose models that fit
available hardware.

## Supported layers

| Layer | Examples | Contract |
|---|---|---|
| Native runtime | DAG and `loops.*` | Vera owns planning, tools, state, and events |
| Agent bridge | smolagents, LangGraph, PydanticAI | Vera owns launch/policy; framework owns its internal run |
| Coding bridge | Claude Code, Codex, remote IDE agents | Vera owns work item, branch, capacity, and handoff |
| Provider | hosted chat/model APIs | Sealed credentials, model discovery, usage, and cost |
| Catalog | Hugging Face/Ollama metadata | Search, fit estimation, and installation handoff |

## Launch lifecycle

1. Inspect bridge/provider status and dependencies.
2. Resolve a model and verify it is available to the selected runtime.
3. Supply a bounded goal, allowed tools, and execution limits.
4. Start the run and retain its Vera run/session identity.
5. Stream or poll normalized status while preserving framework-native detail.
6. Record terminal output, usage, artifacts, and errors.

Image `ensure` capabilities prepare runtime environments but should not silently
upgrade an active workload. Pin versions for repeatability. A bridge being
installed does not mean its provider credentials, model, or tools are valid.

## Model selection and accounting

Catalog fit is an estimate derived from model metadata, quantization, and known
hardware. Confirm with a real load/warm-up before routing important traffic.
Provider usage should retain provider, model, token counts, price revision, and
request identity. Updating a pricing table changes estimates, not historical
provider invoices.

## Trust boundary

External runtimes may propose tool calls or return structured events, but Vera's
capability policy remains authoritative. Do not grant a framework every tool
merely because it runs in a container. Scope filesystem/network access, pass
secrets by reference, and treat framework output as untrusted until validated.

## Troubleshooting

Separate dependency/image failure, provider authentication, model lookup,
framework initialization, tool-schema incompatibility, runtime exception, and
result-normalization failure. Preserve the native trace alongside Vera's
normalized error; collapsing everything to “agent failed” removes the evidence
needed to fix it.

## Source map

- `vera/agentbridges/` — catalog, environment, and launch normalization.
- `vera/smolagents/`, `vera/langgraph/`, `vera/pydanticai/` — adapters.
- `vera/providers/` — credentials, models, chat, pricing, and usage.
- `vera/catalog/` — discovery and hardware-fit estimates.
- `vera/ide/` and `vera/board/` — coding-agent execution and work ownership.

<!-- VERA:AUTO:screenshots START -->
<!-- VERA:AUTO:screenshots END -->

<!-- VERA:AUTO:capabilities START -->
<!-- VERA:AUTO:capabilities END -->
