"""Opt-in live evidence run for one portable Ollama alias.

The runner persists only model identity, route facts, hashes and outcomes.  It
never enables the alias for application traffic and never includes prompts or
generated content in its report.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from .admission import ModelDeploymentTarget
from .inference_alias_canary import (
    InferenceAliasCanaryPaths,
    InferenceAliasCanarySession,
)
from .inference_health import InferenceProviderHealth
from .live_inference_validation import (
    discover_ollama_artifact,
    ollama_validation_binding,
    ollama_validation_request,
    require_shared_gate,
    validate_ollama_provider,
)


Clock = Callable[[], int]


async def run_live_alias_canary(
    *, model: str, instance_id: str, alias: str, state_dir: Path,
    source_commit: str, source_operation: str, runner: Callable[..., Awaitable[str]],
    gate: dict[str, Any], artifact: dict[str, Any],
    clock: Clock = lambda: int(time.time() * 1000),
) -> dict[str, Any]:
    """Validate, bind, rehearse failure/rollback, and report readiness."""
    require_shared_gate(gate, instance_id)
    node = next(item for item in gate["nodes"] if item.get("node") == instance_id)
    package, provider = ollama_validation_binding(
        model=model, instance_id=instance_id,
        artifact_sha256=artifact["sha256"],
        artifact_size=artifact["size_bytes"], runner=runner)
    request = ollama_validation_request(
        package, prompt="Reply with VERA_OK only.", stream=False,
        content_mode="exact_text")
    started_ms = clock()
    health = InferenceProviderHealth(
        provider.profile().provider_id, "ready", started_ms,
        started_ms + 300_000, "live-validator", (package.package_id,),
        concurrency_limit=int(node["capacity"]))
    placement = "gpu" if node.get("has_gpu") else "cpu"
    target = ModelDeploymentTarget(
        instance_id, "generate", "prompt/v1", "text/v1",
        max(1, int(artifact["size_bytes"])), accelerators=(placement,),
        frameworks=("ollama",))
    session = InferenceAliasCanarySession(
        InferenceAliasCanaryPaths.under(state_dir))
    binding = session.prepare(
        alias=alias, package=package, provider=provider, health=health,
        request=request, target=target, source_operation=source_operation,
        as_of_ms=started_ms, placements=(placement,))
    live_report = await validate_ollama_provider(
        model=model, instance_id=instance_id,
        artifact_sha256=artifact["sha256"],
        artifact_size=artifact["size_bytes"], runner=runner,
        validate_cancellation=True)
    observed_ms = clock()
    live_evidence = session.record_live_report(
        binding, live_report, source_commit=source_commit,
        observed_at_ms=observed_ms)
    outage = await session.rehearse_provider_outage(
        binding, request, provider=provider, health=health,
        source_commit=source_commit, observed_at_ms=clock())
    rollback = session.rehearse_rollback(
        binding, source_commit=source_commit, observed_at_ms=clock())
    readiness = session.readiness(
        binding.route, source_commit=source_commit,
        as_of_ms=clock(), max_age_ms=300_000)
    return {
        "schema": "vera.live-alias-canary/v1",
        "alias": binding.route.alias,
        "route_id": binding.route.route_id,
        "model_package_id": package.package_id,
        "provider_id": provider.profile().provider_id,
        "live_report": live_report,
        "evidence_ids": [item.evidence_id for item in
                         (*live_evidence, outage, rollback)],
        "readiness": readiness.to_dict(),
        "traffic_activation_allowed": False,
        "privacy": "prompt_and_output_omitted",
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    from ..capability_orchestration import ollama_generate, ollama_gate_status

    gate = await ollama_gate_status()
    artifact = await discover_ollama_artifact(args.instance, args.model)
    return await run_live_alias_canary(
        model=args.model, instance_id=args.instance, alias=args.alias,
        state_dir=Path(args.state_dir), source_commit=args.source_commit,
        source_operation=args.operation, runner=ollama_generate, gate=gate,
        artifact=artifact)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a bounded, non-activating portable alias canary")
    parser.add_argument("--model", required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--alias", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--operation", required=True)
    args = parser.parse_args()
    try:
        report = asyncio.run(_run(args))
    except (RuntimeError, TypeError, ValueError):
        report = {
            "schema": "vera.live-alias-canary/v1", "status": "refused",
            "ready_for_activation_review": False,
            "traffic_activation_allowed": False,
            "privacy": "prompt_and_output_omitted",
        }
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report.get("readiness", {}).get(
        "ready_for_activation_review") else 1


if __name__ == "__main__":
    raise SystemExit(main())
