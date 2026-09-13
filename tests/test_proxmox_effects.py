import json

import pytest

import Vera.vera.integrations.infrastructure_effects as effects
import Vera.vera.proxmox.proxmox_capabilities as proxmox


pytestmark = pytest.mark.critical


@pytest.fixture
def harness(monkeypatch):
    order = []
    observations = []
    provider_calls = []

    async def get_cluster(*_args, **_kwargs):
        return {"id": "cluster:private", "api_url": "https://private.invalid"}

    def observe(**kwargs):
        order.append("observe")
        observations.append(kwargs)
        return {"enforcement": "observe_only", "delivery": {"mode": kwargs["mode"]}}

    async def pve(_rec, method, path, data=None):
        order.append("provider")
        provider_calls.append({"method": method, "path": path, "data": data})
        if path == "/cluster/nextid":
            return 999, ""
        if path.endswith("/agent/exec"):
            return {"pid": 7}, ""
        if "exec-status" in path:
            return {"exited": True, "exitcode": 0, "out-data": "private"}, ""
        return "UPID", ""

    async def emit(*_args, **_kwargs):
        return None

    async def wait(*_args, **_kwargs):
        return None

    monkeypatch.setattr(proxmox, "_get_cluster", get_cluster)
    monkeypatch.setattr(proxmox, "_observe_proxmox_effect", observe)
    monkeypatch.setattr(proxmox, "_pve", pve)
    monkeypatch.setattr(proxmox, "emit_event", emit)
    monkeypatch.setattr(proxmox, "_wait_pve_task", wait)
    return order, observations, provider_calls


def _assert_one_shadow(result, harness, mode):
    order, observations, _calls = harness
    assert order[0:2] == ["observe", "provider"]
    assert len(observations) == 1
    assert observations[0]["mode"] == mode
    assert observations[0]["idempotency_key"] == "key:private"
    assert observations[0]["approval_receipt_ref"] == "approval:private"
    assert observations[0]["retry"] is True
    assert result["effect_shadow"]["enforcement"] == "observe_only"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fn", "mode", "kwargs"),
    [
        (proxmox.cap_guest_action, "guest_action",
         {"node": "pve", "guest_type": "qemu", "vmid": 10, "action": "start"}),
        (proxmox.cap_guest_clone, "guest_clone",
         {"node": "pve", "guest_type": "qemu", "vmid": 10, "newid": 11}),
        (proxmox.cap_vm_create, "vm_create",
         {"node": "pve", "template_vmid": 10, "newid": 11, "start": False}),
        (proxmox.cap_lxc_create, "lxc_create",
         {"node": "pve", "vmid": 11, "ostemplate": "private", "start": False}),
        (proxmox.cap_guest_destroy, "guest_destroy",
         {"node": "pve", "guest_type": "lxc", "vmid": 10}),
        (proxmox.cap_fw_rule_add, "firewall_rule_add",
         {"scope": "guest", "node": "pve", "guest_type": "qemu", "vmid": 10,
          "source": "private", "comment": "private"}),
        (proxmox.cap_fw_rule_delete, "firewall_rule_delete",
         {"scope": "guest", "node": "pve", "guest_type": "qemu", "vmid": 10,
          "pos": 1}),
    ],
)
async def test_pve_mutations_observe_once_before_first_provider_call(
        fn, mode, kwargs, harness):
    result = await fn.__wrapped__(
        cluster_id="cluster:private", idempotency_key="key:private",
        approval_receipt_ref="approval:private", retry=True, **kwargs)
    _assert_one_shadow(result, harness, mode)
    encoded_calls = json.dumps(harness[2])
    assert "key:private" not in encoded_calls
    assert "approval:private" not in encoded_calls


@pytest.mark.asyncio
async def test_lxc_guest_exec_observes_before_ssh_without_forwarding_controls(
        monkeypatch, harness):
    async def run(**kwargs):
        harness[0].append("provider")
        harness[2].append(kwargs)
        return {"ok": True, "rc": 0, "stdout": "private"}

    monkeypatch.setattr(proxmox, "_cap", lambda name: run if name == "exec.ssh.run" else None)
    result = await proxmox.cap_guest_exec.__wrapped__(
        cluster_id="cluster:private", node="pve", guest_type="lxc", vmid=10,
        command="private command", pve_ssh_host_id="host:private",
        idempotency_key="key:private", approval_receipt_ref="approval:private",
        retry=True)
    _assert_one_shadow(result, harness, "guest_exec")
    assert set(harness[2][0]) == {"host_id", "command", "timeout"}


@pytest.mark.asyncio
async def test_qemu_guest_exec_observes_once_before_agent_call(harness):
    result = await proxmox.cap_guest_exec.__wrapped__(
        cluster_id="cluster:private", node="pve", guest_type="qemu", vmid=10,
        command="private command", idempotency_key="key:private",
        approval_receipt_ref="approval:private", retry=True)
    _assert_one_shadow(result, harness, "guest_exec")


@pytest.mark.asyncio
async def test_node_exec_observes_before_ssh_without_forwarding_controls(
        monkeypatch, harness):
    async def run(**kwargs):
        harness[0].append("provider")
        harness[2].append(kwargs)
        return {"ok": True, "rc": 0}

    monkeypatch.setattr(proxmox, "_cap", lambda name: run if name == "exec.ssh.run" else None)
    result = await proxmox.cap_node_exec.__wrapped__(
        cluster_id="cluster:private", node="pve", command="private command",
        pve_ssh_host_id="host:private", idempotency_key="key:private",
        approval_receipt_ref="approval:private", retry=True)
    _assert_one_shadow(result, harness, "node_exec")
    assert set(harness[2][0]) == {"host_id", "command", "timeout"}


@pytest.mark.asyncio
async def test_validation_failure_does_not_claim_an_effect(monkeypatch):
    monkeypatch.setattr(
        proxmox, "_observe_proxmox_effect",
        lambda **_kwargs: pytest.fail("invalid request must not be observed"))
    result = await proxmox.cap_guest_action.__wrapped__(action="invalid")
    assert "error" in result


def test_proxmox_shadow_is_payload_free(monkeypatch):
    monkeypatch.setattr(proxmox, "observe_infrastructure_effect",
                        effects.plan_infrastructure_effect)
    result = proxmox._observe_proxmox_effect(
        rec={"id": "cluster:private"}, mode="guest_exec",
        resource_ref="resource:private", operation={"command": "secret command"},
        idempotency_key="key:private", approval_receipt_ref="approval:private")
    encoded = json.dumps(result)
    assert result["delivery"]["provider"] == "proxmox"
    assert result["delivery"]["mode"] == "guest_exec"
    for raw in ("cluster:private", "resource:private", "secret command",
                "key:private", "approval:private"):
        assert raw not in encoded
