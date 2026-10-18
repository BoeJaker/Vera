from vera.intel import intel_effect_inventory as effects
from vera.intel import intel_core


def test_every_current_channel_and_action_is_classified():
    result = effects.project(intel_core.CHANNELS, intel_core.ACTIONS)
    by_id = {row["id"]: row for row in result["boundaries"]}
    assert {f"channel.{name}" for name in intel_core.CHANNELS} <= set(by_id)
    assert {f"action.{name}" for name in intel_core.ACTIONS} <= set(by_id)
    assert all("classification_missing" not in row["gaps"] for row in by_id.values())


def test_downstream_shadow_is_not_misreported_as_intel_attributable():
    result = effects.project(intel_core.CHANNELS, intel_core.ACTIONS,
                             available_caps=["tg.send_rich", "mail.send"])
    by_id = {row["id"]: row for row in result["boundaries"]}
    assert by_id["channel.telegram"]["observation"] == "downstream_shadow"
    assert by_id["channel.telegram"]["evidence_family"] == "telegram"
    assert by_id["channel.telegram"]["gaps"] == ["intel_attribution_missing"]
    assert by_id["channel.email"]["available"] is True
    assert result["readiness"]["intel_attributable"] is False
    assert result["claims"]["records_evidence"] is False


def test_unobserved_external_paths_remain_explicit_gaps():
    result = effects.project(intel_core.CHANNELS, intel_core.ACTIONS)
    by_id = {row["id"]: row for row in result["boundaries"]}
    for boundary in ("channel.phone", "channel.print", "action.repo",
                     "repo.acquire", "n8n.proposal.create", "n8n.retirement.activate"):
        assert by_id[boundary]["external"] is True
        assert by_id[boundary]["gaps"] == ["portable_effect_observation_missing"]


def test_source_filtered_evidence_makes_provider_observations_attributable():
    result = effects.project(
        intel_core.CHANNELS, intel_core.ACTIONS,
        attributed_evidence={
            "telegram": {"totals": {"observations": 3}, "source_filter": "intel"},
            "email": {"totals": {"observations": 2}, "source_filter": "intel"},
        })
    assert result["readiness"]["intel_attributable"] is True
    assert result["readiness"]["attributed_observations"] == 5
    assert result["evidence"]["telegram"]["source_filter"] == "intel"
    by_id = {row["id"]: row for row in result["boundaries"]}
    assert by_id["channel.telegram"]["gaps"] == []
    assert "intel_attribution_missing" not in result["gaps"]


def test_available_attribution_with_no_receipts_is_not_misreported_as_coverage():
    result = effects.project(
        intel_core.CHANNELS, intel_core.ACTIONS,
        attributed_evidence={"telegram": {"totals": {"observations": 0},
                                                   "source_filter": "intel"}})
    by_id = {row["id"]: row for row in result["boundaries"]}
    assert by_id["channel.telegram"]["gaps"] == [
        "provider_family_has_no_intel_observations"]


def test_unknown_future_boundary_fails_visible_not_silent():
    result = effects.project({"new-channel": {"cap": "new.send"}}, {})
    row = result["boundaries"][0]
    assert row["classification"] == "unknown"
    assert row["observation"] == "unclassified"
    assert row["gaps"] == ["classification_missing"]
