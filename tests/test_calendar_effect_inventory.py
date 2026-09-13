from pathlib import Path

import pytest

import Vera.vera.calendar.calendar_capabilities as calendar
from Vera.vera.calendar.effect_inventory import calendar_effect_inventory
from Vera.vera.integrations.effect_shadow_evidence import EVIDENCE_FAMILIES


pytestmark = pytest.mark.critical


def test_inventory_separates_local_state_remote_reads_and_authorization():
    result = calendar_effect_inventory()
    assert result["local_state"]["external_effect"] is False
    assert "event.upsert" in result["local_state"]["operations"]
    assert result["remote_reads"] == {
        "operations": ["ics.fetch", "caldav.report", "google.events.list"],
        "direction": "inbound_sync", "external_mutation": False}
    assert result["authorization"]["calendar_mutation"] is False


def test_inventory_does_not_claim_missing_remote_write_coverage():
    result = calendar_effect_inventory()
    assert result["remote_mutations"] == {
        "implemented": False, "operations": [], "effect_family": "calendar",
        "evidence_available": False, "reason": "no_remote_calendar_write_adapter"}
    assert result["claims"] == {
        "external_effect_contract_applied": False,
        "enforcement_available": False, "retries_added": False,
        "executes": False, "probes": False}
    assert "calendar" not in EVIDENCE_FAMILIES


@pytest.mark.asyncio
async def test_status_capability_is_deterministic_and_nonexecuting():
    result = await calendar.cap_effects_status.__wrapped__()
    assert result == calendar_effect_inventory()
    assert result["claims"]["executes"] is False


def test_calendar_ui_exposes_honest_boundary_status():
    source = (Path(__file__).resolve().parents[1] / "vera" / "calendar" /
              "calendar_panel.html").read_text(encoding="utf-8")
    assert "/cal/effects/status" in source
    assert "local edits · remote sync read-only" in source
    assert "No remote calendar-write adapter is implemented" in source


def test_inventory_matches_current_calendar_transport_surface():
    source = (Path(__file__).resolve().parents[1] / "vera" / "calendar" /
              "calendar_capabilities.py").read_text(encoding="utf-8")
    assert '"REPORT", url' in source
    assert "resp = await c.get(" in source
    assert 'f"{GOOGLE_API}/calendars/' in source
    assert "await c.post(GOOGLE_TOKEN" in source
    for remote_write in (
            'c.post(f"{GOOGLE_API}/calendars',
            'c.put(f"{GOOGLE_API}/calendars',
            'c.patch(f"{GOOGLE_API}/calendars',
            'c.delete(f"{GOOGLE_API}/calendars'):
        assert remote_write not in source
