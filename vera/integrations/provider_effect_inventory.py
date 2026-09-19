"""Deterministic inventory of commerce and infrastructure effect boundaries.

This module describes existing code.  It deliberately imports no provider,
opens no connection or credential, and does not imply that an operation has
been migrated to Vera's shared external-effect contract.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


SCHEMA = "vera.provider-effect-inventory/v1"

_DOMAINS: tuple[dict[str, Any], ...] = (
    {
        "id": "business",
        "label": "Business workspace",
        "boundary": "local_state_and_simulation",
        "operations": {
            "local_mutations": [
                "accounts", "transactions", "invoices", "expenses",
                "inventory", "orders", "customers", "stores",
            ],
            "simulations": ["business.sim.*"],
            "remote_reads": [],
            "remote_mutations": [],
        },
        "authority": "vera_local_databases",
        "external_mutation": False,
        "source_modules": [
            "vera/business/business_capabilities.py",
            "vera/business/business_sim.py",
            "vera/commerce/commerce_capabilities.py",
        ],
    },
    {
        "id": "commerce_marketplaces",
        "label": "Commerce marketplaces",
        "boundary": "mixed_external_provider",
        "operations": {
            "local_mutations": [
                "business.platform.connect", "business.platform.disconnect",
            ],
            "credential_lifecycle": [
                "ebay.oauth.exchange", "ebay.oauth.refresh",
            ],
            "remote_reads": [
                "business.platform.listings.sync",
                "business.platform.orders.sync",
                "commerce pricing/enrichment fetches",
            ],
            "remote_mutations": [
                "business.platform.listing.push",
                "business.listing.publish",
                "business.listing.archive",
            ],
            "declared_unimplemented": [
                "shopify", "woocommerce", "etsy", "amazon",
            ],
        },
        "authority": "marketplace_provider_for_remote_listing_state",
        "external_mutation": True,
        "effect_family": "commerce",
        "effect_contract_applied": True,
        "effect_enforcement": "observe_only",
        "automatic_retries": False,
        "source_modules": [
            "vera/commerce/commerce_platforms.py",
            "vera/commerce/commerce_enrich.py",
            "vera/commerce/commerce_listing.py",
            "vera/commerce/commerce_market.py",
        ],
    },
    {
        "id": "container_and_build",
        "label": "Container and build infrastructure",
        "boundary": "local_or_registered_engine_mutation",
        "operations": {
            "local_registry": ["docker.hosts.save", "docker.hosts.delete"],
            "remote_reads": [
                "docker.ping", "docker.ps", "docker.images",
                "docker.stats.top", "docker.stack.status", "build.status",
            ],
            "remote_mutations": [
                "docker.exec", "docker.stop", "docker.rm", "docker.run",
                "docker.image.ensure", "docker.worker.spawn",
                "docker.worker.stop", "docker.stack.deploy",
                "build.builder.up", "build.arduino", "build.platformio",
                "build.run", "build.python",
            ],
        },
        "authority": "selected_docker_engine_or_builder",
        "external_mutation": True,
        "effect_family": "infrastructure",
        "effect_contract_applied": False,
        "effect_observation": "partial",
        "observed_mutations": [
            "docker.exec", "docker.stop", "docker.rm", "docker.run",
            "docker.worker.stop", "docker.image.ensure", "docker.worker.spawn",
            "docker.stack.deploy",
            "build.builder.up", "build.arduino", "build.platformio",
            "build.run", "build.python",
        ],
        "automatic_retries": False,
        "source_modules": [
            "vera/workers/docker_capabilities.py",
            "vera/build/build_capabilities.py",
        ],
    },
    {
        "id": "proxmox_and_provisioning",
        "label": "Proxmox and managed-host provisioning",
        "boundary": "remote_infrastructure_mutation",
        "operations": {
            "local_registry": [
                "proxmox.cluster.save", "proxmox.cluster.delete",
                "provisioning configuration and identity registry edits",
            ],
            "remote_reads": [
                "proxmox.status", "proxmox.guest.ip", "proxmox.nextid",
                "proxmox.storage.content", "proxmox.fw.rules.list",
                "provision status/detect operations",
            ],
            "remote_mutations": [
                "proxmox.guest.action", "proxmox.guest.exec",
                "proxmox.node.exec", "proxmox.guest.clone",
                "proxmox.vm.create", "proxmox.lxc.create",
                "proxmox.guest.destroy", "proxmox.fw.rule.add",
                "proxmox.fw.rule.delete", "provision.deploy",
                "provision.install", "provision.run", "provision.store.deploy",
                "provision.store.remove", "provision.security.deploy",
                "provision.security.remove",
            ],
        },
        "authority": "selected_proxmox_cluster_or_managed_host",
        "external_mutation": True,
        "effect_family": "infrastructure",
        "effect_contract_applied": False,
        "effect_observation": "complete",
        "observed_mutations": [
            "proxmox.guest.action", "proxmox.guest.exec",
            "proxmox.node.exec", "proxmox.guest.clone",
            "proxmox.vm.create", "proxmox.lxc.create",
            "proxmox.guest.destroy", "proxmox.fw.rule.add",
            "proxmox.fw.rule.delete",
            "provision.deploy", "provision.install", "provision.run",
            "provision.store.deploy", "provision.store.remove",
            "provision.security.deploy", "provision.security.remove",
        ],
        "automatic_retries": False,
        "source_modules": [
            "vera/proxmox/proxmox_capabilities.py",
            "vera/proxmox/pxstore_capabilities.py",
            "vera/provisioning/",
        ],
    },
)


def provider_effect_inventory() -> dict[str, Any]:
    """Return a fresh, bounded snapshot without inspecting runtime state."""
    domains = deepcopy(list(_DOMAINS))
    mutating = [item["id"] for item in domains if item["external_mutation"]]
    return {
        "schema": SCHEMA,
        "domains": domains,
        "summary": {
            "domains": len(domains),
            "external_mutation_domains": mutating,
            "migrated_effect_families": ["commerce"],
            "enforcement_available": False,
        },
        "next_adapter": {
            "domain": "",
            "providers": [],
            "reason": "inventory_declared_remote_mutations_observed",
            "required_before_enforcement": [
                "payload_free_shadow_evidence",
                "operation_specific_approval",
                "provider_idempotency_and_receipt_analysis",
                "credentialed_live_validation_separately_authorized",
            ],
        },
        "claims": {
            "executes": False,
            "probes": False,
            "opens_credentials": False,
            "records_receipts": False,
            "adds_retries": False,
            "changes_enforcement": False,
        },
    }
