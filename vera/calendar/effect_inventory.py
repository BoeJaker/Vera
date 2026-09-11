"""Deterministic inventory of Calendar state and external-effect boundaries."""
from __future__ import annotations

from typing import Any


SCHEMA = "vera.calendar-effect-inventory/v1"


def calendar_effect_inventory() -> dict[str, Any]:
    """Describe implemented boundaries without probing or performing an operation."""
    return {
        "schema": SCHEMA,
        "local_state": {
            "operations": [
                "event.upsert", "event.delete", "todo.upsert", "todo.toggle",
                "todo.delete", "note.upsert", "note.delete", "braindump.commit",
            ],
            "authority": "vera_redis",
            "external_effect": False,
        },
        "remote_reads": {
            "operations": ["ics.fetch", "caldav.report", "google.events.list"],
            "direction": "inbound_sync",
            "external_mutation": False,
        },
        "authorization": {
            "operations": ["google.oauth.exchange", "google.oauth.refresh"],
            "kind": "credential_lifecycle",
            "calendar_mutation": False,
        },
        "remote_mutations": {
            "implemented": False,
            "operations": [],
            "effect_family": "calendar",
            "evidence_available": False,
            "reason": "no_remote_calendar_write_adapter",
        },
        "claims": {
            "external_effect_contract_applied": False,
            "enforcement_available": False,
            "retries_added": False,
            "executes": False,
            "probes": False,
        },
    }
