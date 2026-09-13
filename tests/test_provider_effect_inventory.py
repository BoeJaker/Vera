from pathlib import Path

import pytest

import Vera.vera.integrations.integrations_capabilities as integrations
from Vera.vera.integrations.effect_shadow_evidence import EVIDENCE_FAMILIES
from Vera.vera.integrations.provider_effect_inventory import provider_effect_inventory


pytestmark = pytest.mark.critical
ROOT = Path(__file__).resolve().parents[1]


def _domain(result, domain_id):
    return next(item for item in result["domains"] if item["id"] == domain_id)


def test_inventory_separates_local_business_from_real_provider_mutations():
    result = provider_effect_inventory()
    business = _domain(result, "business")
    commerce = _domain(result, "commerce_marketplaces")
    assert business["boundary"] == "local_state_and_simulation"
    assert business["external_mutation"] is False
    assert business["operations"]["remote_mutations"] == []
    assert commerce["external_mutation"] is True
    assert commerce["operations"]["remote_mutations"] == [
        "business.platform.listing.push",
        "business.listing.publish",
        "business.listing.archive",
    ]
    assert commerce["operations"]["declared_unimplemented"] == [
        "shopify", "woocommerce", "etsy", "amazon"]


def test_inventory_does_not_claim_unmigrated_enforcement_or_retry():
    result = provider_effect_inventory()
    assert result["summary"]["migrated_effect_families"] == ["commerce"]
    assert result["summary"]["enforcement_available"] is False
    assert result["claims"] == {
        "executes": False, "probes": False, "opens_credentials": False,
        "records_receipts": False, "adds_retries": False,
        "changes_enforcement": False,
    }
    commerce = _domain(result, "commerce_marketplaces")
    assert commerce["effect_contract_applied"] is True
    assert commerce["effect_enforcement"] == "observe_only"
    assert "commerce" in EVIDENCE_FAMILIES
    infrastructure = _domain(result, "container_and_build")
    assert infrastructure["effect_contract_applied"] is False
    assert infrastructure["effect_observation"] == "partial"
    assert infrastructure["observed_mutations"] == [
        "docker.exec", "docker.stop", "docker.rm", "docker.run",
        "docker.worker.stop"]
    assert "infrastructure" in EVIDENCE_FAMILIES


def test_inventory_returns_an_independent_snapshot():
    first = provider_effect_inventory()
    first["domains"][0]["operations"]["local_mutations"].append("poison")
    assert "poison" not in provider_effect_inventory()["domains"][0]["operations"]["local_mutations"]


@pytest.mark.asyncio
async def test_status_capability_is_deterministic_and_nonexecuting():
    assert await integrations.integration_effect_inventory.__wrapped__() == provider_effect_inventory()


def test_inventory_matches_current_ebay_write_surface():
    source = (ROOT / "vera" / "commerce" / "commerce_platforms.py").read_text(encoding="utf-8")
    assert 'async def push_listing(' in source
    assert 'async def publish_listing(' in source
    assert 'async def archive_listing(' in source
    assert '/sell/inventory/v1/inventory_item/{sku}' in source
    assert '/publish"' in source
    assert '/withdraw"' in source
    vinted = (ROOT / "vera" / "commerce" / "commerce_vinted.py").read_text(encoding="utf-8")
    assert "class VintedConnector" in vinted
    assert "c.post(f\"https://{sess['domain']}/api/v2/item_upload/items\"" in vinted
    assert "c.delete(f\"https://{sess['domain']}/api/v2/items/{external_id}\"" in vinted


def test_inventory_matches_current_infrastructure_mutation_surface():
    docker = (ROOT / "vera" / "workers" / "docker_capabilities.py").read_text(encoding="utf-8")
    proxmox = (ROOT / "vera" / "proxmox" / "proxmox_capabilities.py").read_text(encoding="utf-8")
    for name in ("docker.exec", "docker.stop", "docker.run", "docker.stack.deploy"):
        assert f'"{name}"' in docker
    for name in ("proxmox.guest.action", "proxmox.guest.clone", "proxmox.vm.create",
                 "proxmox.lxc.create", "proxmox.guest.destroy", "proxmox.fw.rule.add"):
        assert f'"{name}"' in proxmox


def test_integrations_ui_exposes_inventory_without_rollout_controls():
    source = (ROOT / "vera" / "integrations" / "integrations_panel.html").read_text(encoding="utf-8")
    assert "/integrations/effect/inventory" in source
    assert "Provider boundaries" in source
    assert "Static inventory only" in source
    assert "commerce:'Commerce'" in source
    assert "infrastructure:'Infrastructure'" in source
