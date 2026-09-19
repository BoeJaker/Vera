"""Bounded live validation for shipped portable inference adapters.

The report deliberately excludes prompts and model output.  Live execution is
opt-in through the module CLI and the Ollama path reuses Vera's normal
``ollama_generate`` resource gate rather than contacting generation endpoints
directly.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import time
from typing import Any, Awaitable, Callable

import httpx

from .inference_contracts import InferenceRequest, InferenceValue, consume_inference
from .model_package import ModelArtifact, ModelCompatibility, ModelPackage
from .ollama_inference_adapter import LegacyOllamaInferenceProvider


LIVE_INFERENCE_REPORT_SCHEMA = "vera.live-inference-validation/v2"
_IDENT = re.compile(r"[^A-Za-z0-9._:+/-]+")
Runner = Callable[..., Awaitable[str]]


def _identifier(value: str, fallback: str) -> str:
    cleaned = _IDENT.sub("-", str(value or "").strip()).strip("-./:+")
    return (cleaned or fallback)[:128]


def _output_evidence(result: Any, expected_output: str,
                     expected_sha256: str, content_mode: str) -> dict[str, Any]:
    chunks: list[str] = []
    for value in result.outputs:
        decoded = json.loads(value.json_data)
        if not isinstance(decoded, str):
            raise ValueError("portable Ollama output was not text")
        chunks.append(decoded)
    text = "".join(chunks)
    output_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    transport_passed = bool(result.status == "completed" and text)
    semantic_evidence: dict[str, Any] = {}
    if content_mode == "json_semantic":
        try:
            actual = json.loads(text)
            expected = json.loads(expected_output)
            content_conformant = actual == expected
            semantic_evidence["json_valid"] = True
            semantic_evidence["json_type_matches"] = type(actual) is type(expected)
            if isinstance(actual, dict) and isinstance(expected, dict):
                expected_keys = set(expected)
                actual_keys = set(actual)
                semantic_evidence.update({
                    "expected_field_count": len(expected_keys),
                    "missing_field_count": len(expected_keys - actual_keys),
                    "extra_field_count": len(actual_keys - expected_keys),
                    "matching_value_count": sum(
                        1 for key, value in expected.items()
                        if key in actual and actual[key] == value),
                })
        except (TypeError, ValueError):
            content_conformant = False
            semantic_evidence["json_valid"] = False
    else:
        content_conformant = output_sha256 == expected_sha256
    return {
        "status": result.status,
        "error_code": result.error_code,
        "output_chunks": len(chunks),
        "output_bytes": len(text.encode("utf-8")),
        "output_sha256": output_sha256,
        "expected_output_sha256": expected_sha256,
        "transport_passed": transport_passed,
        "content_conformant": bool(transport_passed and content_conformant),
        "usage": dict(result.usage),
        **semantic_evidence,
    }


def require_shared_gate(gate: Any, instance_id: str) -> str:
    """Require authoritative coordination for the exact inference node.

    The transport may be a local coordinator or the sandbox's restricted
    controller broker. Callers must not infer connectivity from Redis access.
    """
    if not isinstance(gate, dict) or not gate.get("enabled") \
            or not gate.get("coord_connected"):
        raise RuntimeError(
            "shared Ollama coordination gate unavailable; live validation refused")
    nodes = gate.get("nodes")
    selected = next((node for node in nodes if isinstance(node, dict)
                     and node.get("node") == instance_id), None) \
        if isinstance(nodes, list) else None
    capacity = (selected or {}).get("capacity")
    if not selected or not selected.get("gated") \
            or isinstance(capacity, bool) or not isinstance(capacity, int) \
            or capacity < 1:
        raise RuntimeError(
            "selected Ollama node is not shared-gated; live validation refused")
    raw_mode = gate.get("coordination_mode")
    mode = "direct" if raw_mode is None else str(raw_mode).strip()
    if mode not in {"direct", "controller_broker"}:
        raise RuntimeError(
            "unknown Ollama coordination mode; live validation refused")
    return mode


async def validate_ollama_provider(*, model: str, instance_id: str,
                                   artifact_sha256: str, artifact_size: int,
                                   runner: Runner, prompt: str = "Reply with VERA_OK only.",
                                   expected_output: str = "VERA_OK",
                                   content_mode: str = "exact_text",
                                   case_timeout_seconds: float = 60,
                                   validate_cancellation: bool = False,
                                   cancellation_delay_seconds: float = 0.1,
                                   ) -> dict[str, Any]:
    """Run bounded non-stream and stream cases through one portable binding."""
    if isinstance(case_timeout_seconds, bool) or not 0 < case_timeout_seconds <= 120:
        raise ValueError("case timeout must be within 120 seconds")
    if not isinstance(expected_output, str) or not expected_output \
            or len(expected_output.encode("utf-8")) > 16_384:
        raise ValueError("expected output must be non-empty and at most 16384 bytes")
    if content_mode not in {"exact_text", "json_semantic"}:
        raise ValueError("unsupported content mode")
    if content_mode == "json_semantic":
        try:
            if not isinstance(json.loads(expected_output), (dict, list)):
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ValueError("JSON semantic expectation must be an object or array") from exc
    if (isinstance(cancellation_delay_seconds, bool) or
            not 0 < cancellation_delay_seconds <= 5):
        raise ValueError("cancellation delay must be within 5 seconds")
    expected_sha256 = hashlib.sha256(expected_output.encode("utf-8")).hexdigest()
    package = ModelPackage(
        _identifier(model, "ollama-model"), artifact_sha256[:12], "transformer", "gguf",
        (ModelArtifact("registry_manifest", f"ollama://{instance_id}/{model}",
                       artifact_sha256, artifact_size),),
        ModelCompatibility(("generate",), "prompt/v1", "text/v1"),
        framework="ollama", source_revision=artifact_sha256,
    )
    provider = LegacyOllamaInferenceProvider(
        package, provider_id=f"ollama:{_identifier(instance_id, 'node')}",
        model=model, instance_id=instance_id, runner=runner)
    cases = []
    for stream in (False, True):
        request = InferenceRequest(
            package.package_id, "generate", "prompt/v1", "text/v1",
            (InferenceValue.from_json("prompt", prompt),),
            parameters=(("json_mode", content_mode == "json_semantic"),
                        ("max_tokens", 32), ("temperature", 0), ("think", False)),
            stream=stream, max_output_bytes=16_384)
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                consume_inference(provider, request), timeout=case_timeout_seconds)
        except asyncio.TimeoutError:
            cases.append({"case": "stream" if stream else "non_stream",
                          "status": "failed", "error_code": "case_timeout",
                          "transport_passed": False, "content_conformant": False,
                          "passed": False, "expected_output_sha256": expected_sha256,
                          "elapsed_ms": round((time.monotonic() - started) * 1000)})
            break
        evidence = _output_evidence(
            result, expected_output, expected_sha256, content_mode)
        evidence.update({"case": "stream" if stream else "non_stream",
                         "elapsed_ms": round((time.monotonic() - started) * 1000)})
        evidence["passed"] = bool(evidence["transport_passed"]
                                  and evidence["content_conformant"])
        cases.append(evidence)
    cancellation = None
    if validate_cancellation:
        request = InferenceRequest(
            package.package_id, "generate", "prompt/v1", "text/v1",
            (InferenceValue.from_json("prompt", prompt),),
            parameters=(("json_mode", content_mode == "json_semantic"),
                        ("max_tokens", 32), ("temperature", 0), ("think", False)),
            stream=True, max_output_bytes=16_384)
        started = time.monotonic()
        task = asyncio.create_task(consume_inference(provider, request))
        await asyncio.sleep(cancellation_delay_seconds)
        completed_before_cancel = task.done()
        if not completed_before_cancel:
            task.cancel()
        cancellation_observed = False
        try:
            await asyncio.wait_for(task, timeout=case_timeout_seconds)
        except asyncio.CancelledError:
            cancellation_observed = True
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        cancellation = {
            "case": "cancellation",
            "status": ("completed_before_cancel" if completed_before_cancel else
                       "cancelled" if cancellation_observed else "failed"),
            "cancellation_observed": cancellation_observed,
            "task_reaped": task.done(),
            "passed": bool(not completed_before_cancel and
                           cancellation_observed and task.done()),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }

    report = {
        "schema": LIVE_INFERENCE_REPORT_SCHEMA,
        "provider": provider.profile().provider_id,
        "model_package_id": package.package_id,
        "model": model,
        "instance_id": instance_id,
        "artifact_sha256": artifact_sha256,
        "artifact_size": artifact_size,
        "cases": cases,
        "transport_passed": all(case["transport_passed"] for case in cases),
        "content_conformant": all(case["content_conformant"] for case in cases),
        "content_contract": content_mode,
        "passed": all(case["passed"] for case in cases),
        "privacy": "prompt_and_output_omitted",
    }
    if cancellation is not None:
        report["cancellation"] = cancellation
        report["passed"] = bool(report["passed"] and cancellation["passed"])
    return report


async def discover_ollama_artifact(instance_id: str, model: str) -> dict[str, Any]:
    """Read one configured Ollama tag identity; this performs no inference."""
    from ..capability_orchestration import OLLAMA_INSTANCES

    instance = OLLAMA_INSTANCES.get(instance_id)
    if not instance:
        raise ValueError("unknown Ollama instance")
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(f"{instance['url']}/api/tags")
        response.raise_for_status()
        payload = response.json()
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise ValueError("malformed Ollama model inventory")
    match = next((item for item in models if isinstance(item, dict)
                  and item.get("name") == model), None)
    if not match:
        raise ValueError("model is not present on the selected instance")
    digest = str(match.get("digest") or "").removeprefix("sha256:").lower()
    size = match.get("size")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Ollama inventory omitted a canonical model digest")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError("Ollama inventory omitted a valid model size")
    return {"sha256": digest, "size_bytes": size}


async def _run(model: str, instance_id: str) -> dict[str, Any]:
    from ..capability_orchestration import ollama_generate, ollama_gate_status

    gate = await ollama_gate_status()
    require_shared_gate(gate, instance_id)

    artifact = await discover_ollama_artifact(instance_id, model)
    return await validate_ollama_provider(
        model=model, instance_id=instance_id,
        artifact_sha256=artifact["sha256"], artifact_size=artifact["size_bytes"],
        runner=ollama_generate, validate_cancellation=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run bounded portable Ollama validation")
    parser.add_argument("--model", required=True)
    parser.add_argument("--instance", required=True)
    args = parser.parse_args()
    try:
        report = asyncio.run(_run(args.model, args.instance))
    except RuntimeError as exc:
        code = ("coordination_unavailable" if "coordination gate unavailable" in str(exc)
                else "runtime_error")
        report = {"schema": LIVE_INFERENCE_REPORT_SCHEMA, "passed": False,
                  "status": "refused", "error_code": code,
                  "privacy": "prompt_and_output_omitted"}
    except (TypeError, ValueError, httpx.HTTPError):
        report = {"schema": LIVE_INFERENCE_REPORT_SCHEMA, "passed": False,
                  "status": "refused", "error_code": "invalid_runtime_evidence",
                  "privacy": "prompt_and_output_omitted"}
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
