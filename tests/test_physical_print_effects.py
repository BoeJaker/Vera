import sys

import pytest

import Vera.vera.business.thermal_printer_capabilities as printer
from Vera.vera import capability_orchestration as orchestration
from Vera.vera.business.printer_effects import (
    observe_physical_print_effect, plan_physical_print_effect)
from Vera.vera.integrations.effect_observation_context import attributed
from Vera.vera.integrations.effect_shadow_evidence import (
    EVIDENCE_FAMILIES, ExternalEffectShadowEvidence)


pytestmark = pytest.mark.critical


@pytest.mark.parametrize("transport", ["server_usb", "server_serial", "mesh"])
def test_physical_plan_is_endpoint_and_payload_free(transport):
    result = plan_physical_print_effect(
        transport=transport, endpoint_ref="private-device-1",
        idempotency_key="print:1", approval_receipt_ref="approval:1")
    assert result["delivery"]["stage"] == "physical_attempt"
    assert result["enforcement"] == "observe_only"
    assert result["records_completion"] is False
    assert result["blocks_current_call"] is False
    encoded = str(result)
    assert "private-device-1" not in encoded
    assert "print:1" not in encoded
    assert "approval:1" not in encoded


def test_webserial_is_not_a_physical_provider_plan():
    with pytest.raises(ValueError, match="unsupported"):
        plan_physical_print_effect(transport="webserial", endpoint_ref="browser")


def test_printing_has_separate_source_attributable_evidence_family(monkeypatch, tmp_path):
    assert "printing" in EVIDENCE_FAMILIES
    evidence = ExternalEffectShadowEvidence(tmp_path / "printing.sqlite3")
    monkeypatch.setattr(
        "Vera.vera.business.printer_effects.default_external_effect_shadow_evidence",
        lambda family="": evidence)
    with attributed("intel"):
        observe_physical_print_effect(
            transport="server_usb", endpoint_ref="private-port")
    summary = evidence.summary(source="intel")
    assert summary["totals"]["observations"] == 1
    assert summary["recent"][0]["source"] == "intel"
    assert "private-port" not in str(summary)


def test_intel_print_entry_capability_redacts_content_and_returned_bytes():
    cap = orchestration.CAPABILITY_REGISTRY["print.text"]
    assert set(cap["redact_args"]) == {"text", "printer_id", "title"}
    assert cap["redact_result"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("transport,writer", [
    ("server_usb", "_raw_write"), ("server_serial", "_serial_write")])
async def test_server_transport_observes_immediately_before_device_write(
        monkeypatch, transport, writer):
    order = []

    def observe(**kwargs):
        order.append(("observe", kwargs["transport"], kwargs["endpoint_ref"]))

    async def run(fn, *args):
        order.append(("write", fn.__name__, args[0]))
        return {"ok": True}

    monkeypatch.setattr(printer.printer_effects, "observe_physical_print_effect", observe)
    monkeypatch.setattr(printer, "_run", run)
    result = await printer._route(
        {"transport": transport, "port": "private-port", "baud": 9600}, b"secret-bytes")
    assert order == [
        ("observe", transport, "private-port"), ("write", writer, "private-port")]
    assert result["routed"] is True


@pytest.mark.asyncio
async def test_mesh_observes_before_forward_without_retaining_payload(monkeypatch):
    order = []

    class Mesh:
        @staticmethod
        async def cap_mesh_send(**kwargs):
            order.append(("mesh", kwargs))
            return {"ok": True}

    def observe(**kwargs):
        order.append(("observe", kwargs))

    monkeypatch.setattr(printer.printer_effects, "observe_physical_print_effect", observe)
    monkeypatch.setitem(sys.modules, "mesh_capabilities", Mesh)
    result = await printer._route(
        {"transport": "mesh", "node_id": "private-node", "baud": 9600}, b"secret-bytes")
    assert order[0] == ("observe", {"transport": "mesh", "endpoint_ref": "private-node"})
    assert order[1][0] == "mesh"
    assert "secret-bytes" not in str(order[0])
    assert result["routed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("printer_config", [None, {"transport": "webserial"},
                                               {"transport": "mesh", "node_id": ""}])
async def test_client_handoff_or_unavailable_mesh_never_claims_physical_attempt(
        monkeypatch, printer_config):
    observed = []
    monkeypatch.setattr(
        printer.printer_effects, "observe_physical_print_effect",
        lambda **kwargs: observed.append(kwargs))
    result = await printer._route(printer_config, b"bytes")
    assert observed == []
    assert result["routed"] is False


@pytest.mark.asyncio
async def test_observation_failure_never_blocks_device_write(monkeypatch):
    monkeypatch.setattr(
        printer.printer_effects, "observe_physical_print_effect",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("evidence down")))

    async def run(_fn, *_args):
        return {"ok": True, "wrote": 5}

    monkeypatch.setattr(printer, "_run", run)
    result = await printer._route(
        {"transport": "server_usb", "port": "device"}, b"bytes")
    assert result["routed"] is True
    assert result["wrote"] == 5
