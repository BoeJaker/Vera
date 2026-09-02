import asyncio
import sys

import pytest

from vera.agentbridges import runtime_adapter as runtime


pytestmark = pytest.mark.critical


def _descriptor():
    return runtime.RuntimeAdapterDescriptor(
        runtime_id="fixture-runtime", label="Fixture runtime",
        event_prefix="fixture.run", image="fixture:1",
        package_refs=("fixture==1.0",),
        features=runtime.feature_set({
            name: (("partial", "Fixture explicitly retains a lifecycle gap.")
                   if name == "cancellation"
                   else ("supported", "Fixture deterministic contract evidence."))
            for name in runtime.FEATURES
        }))


def test_inspection_is_complete_stable_and_non_executing():
    before = set(sys.modules)
    adapter = runtime.ContainerRuntimeAdapter(_descriptor())
    result = adapter.inspect()
    assert result == adapter.inspect()
    assert result["schema"] == runtime.SCHEMA
    assert {item["name"] for item in result["features"]} == set(runtime.FEATURES)
    assert result["gaps"] == ["cancellation"]
    assert result["inspection_executes"] is False
    assert result["execution_supported"] is True
    assert isinstance(adapter, runtime.RuntimeAdapter)
    assert set(sys.modules) == before


def test_incomplete_invalid_contracts_fail_closed():
    with pytest.raises(ValueError, match="exactly match"):
        runtime.feature_set({})
    with pytest.raises(ValueError, match="unknown runtime feature"):
        runtime.RuntimeFeature("magic", "supported", "claim")
    with pytest.raises(ValueError, match="feature state"):
        runtime.RuntimeFeature("run", "maybe", "claim")
    with pytest.raises(ValueError, match="package==version"):
        runtime.RuntimeAdapterDescriptor(
            runtime_id="bad-packages", label="Bad packages",
            event_prefix="bad.run", image="bad:1", package_refs=("floating",),
            features=runtime.feature_set({
                name: ("supported", "Complete declaration")
                for name in runtime.FEATURES
            }))
    with pytest.raises(ValueError, match="timeouts"):
        runtime.ContainerRunRequest("run-1", "", ("docker",), 10, 20)
    with pytest.raises(ValueError, match="unsupported characters"):
        runtime.ContainerRunRequest("bad\nrun", "", ("docker",), 10, 2)
    with pytest.raises(ValueError, match="argv"):
        runtime.ContainerRunRequest("run-1", "", tuple("x" for _ in range(129)), 10, 2)


def test_adapter_delegates_validated_run_without_changing_event_contract(monkeypatch):
    captured = {}

    async def fake_runner(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(runtime.bridge, "stream_bridge_container", fake_runner)
    adapter = runtime.ContainerRuntimeAdapter(_descriptor())

    async def emit(event):
        return None

    request = runtime.ContainerRunRequest(
        "run-1", "session-1", ("docker", "run", "fixture:1"),
        timeout_s=30, stall_s=5, progress_kinds=frozenset({"tool_call"}),
        gate_instance_id="gpu-1")
    asyncio.run(adapter.run(request, emit=emit))
    assert captured["event_type_prefix"] == "fixture.run"
    assert captured["argv"] == ["docker", "run", "fixture:1"]
    assert captured["progress_kinds"] == {"tool_call"}
    assert captured["gate_instance_id"] == "gpu-1"
    assert captured["emit"] is emit


def test_adapter_cancellation_delegates_to_exact_runner_registry(monkeypatch):
    async def fake_cancel(run_id):
        return {"ok": True, "accepted": True, "run_id": run_id,
                "state": "cancellation_requested"}

    monkeypatch.setattr(runtime.bridge, "cancel_bridge_run", fake_cancel)
    adapter = runtime.ContainerRuntimeAdapter(_descriptor())
    result = asyncio.run(adapter.cancel("run-1"))
    assert result == {
        "ok": True, "accepted": True, "runtime_id": "fixture-runtime",
        "run_id": "run-1", "state": "cancellation_requested",
    }
    with pytest.raises(ValueError, match="unsupported characters"):
        asyncio.run(adapter.cancel("bad\nrun"))


def test_image_health_and_acquisition_are_separate_from_execution(monkeypatch):
    calls = []

    async def fake_sh(argv, timeout=0):
        calls.append(("health", tuple(argv), timeout))
        return {"ok": True}

    async def fake_present(image):
        calls.append(("present", image))
        return image == "fixture:1"

    async def fake_build(image, dockerfile, context):
        calls.append(("build", image, dockerfile, context))
        return {"ok": True, "present": True, "log": ""}

    monkeypatch.setattr(runtime.bridge, "sh", fake_sh)
    monkeypatch.setattr(runtime.bridge, "image_present", fake_present)
    monkeypatch.setattr(runtime.bridge, "build_image", fake_build)
    adapter = runtime.ContainerRuntimeAdapter(_descriptor())
    health = asyncio.run(adapter.health())
    assert health["docker_ok"] is health["image_present"] is True
    cached = asyncio.run(adapter.ensure_image(dockerfile="Dockerfile", context_dir="."))
    assert cached["action"] == "none"
    built = asyncio.run(adapter.ensure_image(
        dockerfile="Dockerfile", context_dir=".", force=True))
    assert built["action"] == "build"
    assert [call[0] for call in calls].count("build") == 1


@pytest.mark.parametrize(
    "command_result,status,reason,verified",
    [
        ({"ok": False, "out": "", "err": "private daemon detail"},
         "unavailable", "image_inspect_failed", False),
        ({"ok": True, "out": "null", "err": ""},
         "unattested", "image_attestation_invalid", False),
        ({"ok": True, "out": "{}", "err": ""},
         "unattested", "image_attestation_missing", False),
        ({"ok": True, "out": '{"io.vera.runtime.id":"other",'
                               '"io.vera.runtime.packages":"fixture==2.0"}',
          "err": ""}, "mismatch", "image_attestation_mismatch", False),
        ({"ok": True, "out": '{"io.vera.runtime.id":"fixture-runtime",'
                               '"io.vera.runtime.packages":"fixture==1.0"}',
          "err": ""}, "verified", "verified", True),
    ],
)
def test_version_report_is_read_only_bounded_and_honest(
        monkeypatch, command_result, status, reason, verified):
    calls = []

    async def fake_sh(argv, timeout=0):
        calls.append((argv, timeout))
        return command_result

    monkeypatch.setattr(runtime.bridge, "sh", fake_sh)
    report = asyncio.run(runtime.ContainerRuntimeAdapter(_descriptor()).version_report())
    assert report["status"] == status
    assert report["reason_code"] == reason
    assert report["verified"] is verified
    assert report["executes_runtime"] is False
    assert report["trust_level"] == "image_self_declared"
    assert calls == [([
        "docker", "image", "inspect", "fixture:1", "--format",
        "{{json .Config.Labels}}",
    ], 15)]
    assert "private daemon detail" not in str(report)


def test_langgraph_preserves_public_alias_and_uses_runtime_adapter():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "vera" / "langgraph" /
              "langgraph_capabilities.py").read_text(encoding="utf-8")
    assert '"langgraph.run"' in source
    assert "ContainerRuntimeAdapter" in source
    assert "_ADAPTER.run(request" in source
    assert "asyncio.ensure_future(stream_bridge_container" not in source
    assert "build_image, image_present" not in source


def test_langgraph_contract_is_static_and_visible_to_agent_bridge():
    from vera.langgraph.runtime_contract import langgraph_runtime_descriptor
    from vera.agentbridges import agentbridge_capabilities as caps

    descriptor = langgraph_runtime_descriptor().to_dict()
    assert descriptor["runtime_id"] == "langgraph"
    assert descriptor["gaps"] == []
    assert descriptor["package_refs"] == [
        "langchain-core==1.5.5", "langchain-openai==1.5.1",
        "langgraph-prebuilt==1.1.0", "langgraph==1.2.11",
    ]
    result = asyncio.run(caps.agentbridge_interoperability.__wrapped__())
    assert result["runtime_adapters"] == [descriptor]
    assert result["imports_optional_runtimes"] is False


def test_agent_bridge_ui_exposes_adapter_maturity_without_internal_plan_labels():
    from pathlib import Path

    panel = (Path(__file__).parents[1] / "vera" / "agentbridges" /
             "agentbridge_catalog_panel.html").read_text(encoding="utf-8")
    assert "Runtime adapters" in panel
    assert "gaps:" in panel
    assert "join(', ')" in panel
    assert "Check image declaration" in panel
    assert "/agentbridge/runtime/version" in panel
    assert "W3-03" not in panel and "P5-W11" not in panel


def test_runtime_version_capability_routes_only_declared_adapters(monkeypatch):
    from vera.agentbridges import agentbridge_capabilities as caps

    async def fake_report():
        return {"ok": True, "verified": True, "runtime_id": "langgraph"}

    adapter = caps._RUNTIME_ADAPTERS["langgraph"]
    monkeypatch.setattr(adapter, "version_report", fake_report)
    report = asyncio.run(caps.agentbridge_runtime_version.__wrapped__("langgraph"))
    unknown = asyncio.run(caps.agentbridge_runtime_version.__wrapped__("unknown"))
    assert report["verified"] is True
    assert unknown["reason_code"] == "runtime_adapter_unknown"
    assert unknown["executes_runtime"] is False


def test_langgraph_dockerfile_attestation_matches_declared_pins():
    from pathlib import Path
    from vera.langgraph.runtime_contract import langgraph_runtime_descriptor

    dockerfile = (Path(__file__).parents[1] / "vera" / "langgraph" /
                  "Dockerfile.langgraph").read_text(encoding="utf-8")
    descriptor = langgraph_runtime_descriptor()
    assert 'io.vera.runtime.id="langgraph"' in dockerfile
    for package_ref in descriptor.package_refs:
        assert package_ref in dockerfile
