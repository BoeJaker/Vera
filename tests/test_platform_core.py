"""Tests for the platform configuration controller's pure logic.

The weight is on the things that fail silently: a transposed coordinate pair
still parses, an unresolved reference still looks like a configured field, and
deleting a shared value can orphan platforms that referenced it.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vera.platforms import platform_core as pc  # noqa: E402


# ── keys ────────────────────────────────────────────────────────────────────
def test_normalise_key():
    assert pc.normalise_key("Home Coords!") == "home_coords"
    assert pc.normalise_key("  WORK-coords ") == "work_coords"
    assert pc.normalise_key("") == ""


def test_valid_key():
    assert pc.valid_key("home_coords")
    assert not pc.valid_key("Home Coords")
    assert not pc.valid_key("")


# ── coordinates ─────────────────────────────────────────────────────────────
def test_parse_coords_accepts_the_shapes_people_type():
    assert pc.parse_coords("52.234091,0.123136") == (52.234091, 0.123136)
    assert pc.parse_coords("52.234091, 0.123136") == (52.234091, 0.123136)
    assert pc.parse_coords("  52.172872 , 0.112219  ") == (52.172872, 0.112219)


def test_parse_coords_handles_negatives():
    assert pc.parse_coords("-33.8688,151.2093") == (-33.8688, 151.2093)


def test_parse_coords_rejects_junk():
    for bad in ("", "52.234091", "not,coords", "52.234091;0.123136"):
        with pytest.raises(ValueError):
            pc.parse_coords(bad)


def test_parse_coords_range_checks_each_axis():
    # A transposed pair parses fine but is somewhere else entirely, so the
    # range check is the only thing standing between a typo and a travel-time
    # sensor that silently computes nonsense.
    with pytest.raises(ValueError):
        pc.parse_coords("91.0,0.0")
    with pytest.raises(ValueError):
        pc.parse_coords("0.0,181.0")


def test_format_coords_round_trips():
    assert pc.parse_coords(pc.format_coords(52.234091, 0.123136)) == \
        (52.234091, 0.123136)


# ── references ──────────────────────────────────────────────────────────────
def test_is_ref_and_target():
    assert pc.is_ref("@value:home_coords")
    assert pc.is_ref("@secret:ha_token")
    assert not pc.is_ref("52.1,0.1")
    assert pc.ref_target("@value:home_coords") == ("value", "home_coords")
    assert pc.ref_target("@secret:ha_token") == ("secret", "ha_token")


def test_resolve_expands_values_and_secrets():
    got = pc.resolve_fields(
        {"base_url": "http://x", "home_coords": "@value:home_coords",
         "token": "@secret:ha_token"},
        {"home_coords": "52.234091,0.123136"},
        {"ha_token": "abc123"})
    assert got["resolved"]["base_url"] == "http://x"
    assert got["resolved"]["home_coords"] == "52.234091,0.123136"
    assert got["resolved"]["token"] == "abc123"
    assert got["missing"] == []
    assert got["secret_fields"] == ["token"]


def test_resolve_reports_missing_rather_than_blanking_silently():
    """A blank API key that looks configured is worse than an obviously
    absent one."""
    got = pc.resolve_fields({"token": "@secret:nope"}, {}, {})
    assert got["resolved"]["token"] == ""
    assert got["missing"] == [{"field": "token", "kind": "secret",
                               "key": "nope"}]


def test_resolve_treats_empty_stored_value_as_missing():
    got = pc.resolve_fields({"token": "@secret:ha_token"}, {},
                            {"ha_token": ""})
    assert got["missing"][0]["key"] == "ha_token"


def test_redact_only_touches_secret_fields():
    out = pc.redact_fields({"base_url": "http://x", "token": "abc"}, ["token"])
    assert out["base_url"] == "http://x"
    assert out["token"] == "••••••••"


def test_redact_leaves_empty_secret_empty():
    # Redacting "" to bullets would imply a secret is set when it is not.
    assert pc.redact_fields({"token": ""}, ["token"])["token"] == ""


def test_referencing_platforms_finds_the_blast_radius():
    plats = [
        {"id": "homeassistant", "fields": {"home_coords": "@value:home_coords"}},
        {"id": "n8n", "fields": {"timezone": "@value:timezone"}},
    ]
    assert pc.referencing_platforms(plats, "value", "home_coords") == \
        ["homeassistant"]
    assert pc.referencing_platforms(plats, "value", "timezone") == ["n8n"]
    assert pc.referencing_platforms(plats, "value", "unused") == []


# ── Home Assistant payloads ─────────────────────────────────────────────────
def test_ha_core_config_payload():
    b = pc.ha_core_config_payload(52.234091, 0.123136,
                                  timezone="Europe/London", currency="GBP")
    assert b["latitude"] == 52.234091 and b["longitude"] == 0.123136
    assert b["time_zone"] == "Europe/London"
    assert b["unit_system"] == "metric"
    assert b["currency"] == "GBP"


def test_ha_core_config_omits_unset_optionals():
    b = pc.ha_core_config_payload(1.0, 2.0)
    assert "time_zone" not in b and "currency" not in b


def test_waze_flow_data_passes_coordinates_through_verbatim():
    # The integration accepts several shapes and fails quietly on the wrong
    # one; raw "lat,lon" is the shape that always works.
    d = pc.waze_flow_data("52.234091,0.123136", "52.172872,0.112219",
                          name="Home to work")
    assert d["origin"] == "52.234091,0.123136"
    assert d["destination"] == "52.172872,0.112219"
    assert d["region"] == "gb"
    assert d["name"] == "Home to work"


def test_waze_flow_data_validates_both_endpoints():
    with pytest.raises(ValueError):
        pc.waze_flow_data("nonsense", "52.1,0.1")
    with pytest.raises(ValueError):
        pc.waze_flow_data("52.1,0.1", "")


def test_waze_pair_builds_both_directions():
    pair = pc.waze_pair("52.234091,0.123136", "52.172872,0.112219")
    assert len(pair) == 2
    assert pair[0]["origin"] == pair[1]["destination"]
    assert pair[0]["destination"] == pair[1]["origin"]
    assert {p["name"] for p in pair} == {"Home to work", "Work to home"}


# ── platform specs ──────────────────────────────────────────────────────────
def test_new_platform_prewires_shared_values():
    p = pc.new_platform("homeassistant")
    assert p["kind"] == "homeassistant"
    assert p["fields"]["home_coords"] == "@value:home_coords"
    assert p["fields"]["work_coords"] == "@value:work_coords"
    assert p["fields"]["waze_region"] == "gb"
    assert p["fields"]["token"] == ""


def test_new_platform_rejects_unknown_kind():
    with pytest.raises(ValueError):
        pc.new_platform("nope")


def test_secret_field_keys():
    assert pc.secret_field_keys("homeassistant") == ["token"]
    assert pc.secret_field_keys("n8n") == ["api_key"]
    assert pc.secret_field_keys("unknown") == []


def test_config_completeness_names_what_is_missing():
    got = pc.config_completeness("homeassistant",
                                 {"base_url": "http://x", "token": ""})
    assert got["configured"] is False
    assert "Long-lived access token" in got["missing_required"]

    ok = pc.config_completeness("homeassistant",
                                {"base_url": "http://x", "token": "t"})
    assert ok["configured"] is True
    assert ok["missing_required"] == []
