"""Tests for vera/homeassistant/ha_core.py — pure, no app, no network.

Imported as `vera.homeassistant.ha_core` with the worktree on sys.path, NOT as
`Vera.vera.…`: the capital-V namespace package resolves to whatever checkout is
first on the path, which on the host is the MAIN one, so the uppercase import
would silently test code this branch never changed.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.homeassistant import ha_core as core  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
#  URLs — the fault that motivated the module
# ─────────────────────────────────────────────────────────────────────────────

class TestNormaliseBase:
    def test_bare_host_port_gets_a_scheme(self):
        # This exact shape ("192.168.0.94:8096") is what a real integration on
        # this estate was configured with, and the HTTP client rejected every
        # request with "No connection adapters were found".
        assert core.normalise_base("192.168.0.94:8096") == "http://192.168.0.94:8096"

    def test_existing_scheme_is_kept(self):
        assert core.normalise_base("https://ha.local:8123") == "https://ha.local:8123"

    def test_trailing_slash_stripped(self):
        assert core.normalise_base("http://ha.local:8123/") == "http://ha.local:8123"

    def test_empty_stays_empty(self):
        assert core.normalise_base("") == ""
        assert core.normalise_base("   ") == ""

    def test_hostname_alone(self):
        assert core.normalise_base("homeassistant") == "http://homeassistant"

    def test_api_url_has_exactly_one_slash(self):
        assert core.api_url("http://ha:8123/", "/api/states") == \
            "http://ha:8123/api/states"
        assert core.api_url("http://ha:8123", "api/states") == \
            "http://ha:8123/api/states"


# ─────────────────────────────────────────────────────────────────────────────
#  Entities
# ─────────────────────────────────────────────────────────────────────────────

def E(entity_id, state="on", name=None):
    attrs = {"friendly_name": name} if name else {}
    return {"entity_id": entity_id, "state": state, "attributes": attrs}


# Modelled on the real instance: the names are the ones actually present.
STATES = [
    E("light.tcp_smart_bulb", "on", "TCP Smart Bulb"),
    E("light.gaming", "unavailable", "Gaming"),
    E("switch.bedside_lamp_socket_1", "unavailable", "Bedside Lamp Socket 1"),
    E("switch.underbed_lamp_socket_1", "unavailable", "Underbed Lamp Socket 1"),
    E("scene.main_lights_off", "unknown", "Main Lights Off"),
    E("scene.movie_night", "unknown", "Movie Night"),
    E("person.joe_baker", "not_home", "Joe Baker"),
    E("sensor.smasnug_battery_level", "17", "Smasnug Battery Level"),
]


class TestSplitEntity:
    def test_splits(self):
        assert core.split_entity("light.kitchen") == ("light", "kitchen")

    def test_malformed_has_no_domain(self):
        assert core.split_entity("kitchen") == ("", "kitchen")
        assert core.is_entity_id("kitchen") is False
        assert core.is_entity_id("light.kitchen") is True


class TestFriendlyName:
    def test_prefers_friendly_name(self):
        assert core.friendly_name(E("light.x", name="Hall Lamp")) == "Hall Lamp"

    def test_falls_back_to_readable_object_id(self):
        assert core.friendly_name(E("light.hall_lamp")) == "hall lamp"


class TestIsAvailable:
    @pytest.mark.parametrize("state", ["unavailable", "unknown", "none", ""])
    def test_unreachable_states(self, state):
        assert core.is_available(E("light.x", state)) is False

    @pytest.mark.parametrize("state", ["on", "off", "17", "not_home"])
    def test_reachable_states(self, state):
        assert core.is_available(E("light.x", state)) is True

    @pytest.mark.parametrize("domain", ["scene", "button", "script", "event"])
    def test_unknown_is_the_resting_state_of_a_stateless_domain(self, domain):
        # Found live: 18 of 49 entities reported "unavailable" were scenes
        # nobody had activated. A scene has no state until it runs, so calling
        # that a fault hides the devices that are genuinely unreachable.
        assert core.is_available(E(domain + ".x", "unknown")) is True

    def test_a_stateless_domain_can_still_be_unavailable(self):
        assert core.is_available(E("scene.x", "unavailable")) is False

    def test_summary_no_longer_counts_scenes_as_broken(self):
        mixed = [E("scene.a", "unknown"), E("scene.b", "unknown"),
                 E("light.dead", "unavailable")]
        s = core.summarise(mixed)
        assert s["unavailable_count"] == 1
        assert s["unavailable"] == ["light.dead"]


class TestScoreMatch:
    def test_exact_entity_id_wins_outright(self):
        assert core.score_match("light.tcp_smart_bulb", STATES[0]) == 1000

    def test_exact_friendly_name(self):
        assert core.score_match("TCP Smart Bulb", STATES[0]) == 900

    def test_case_insensitive(self):
        assert core.score_match("tcp smart bulb", STATES[0]) == 900

    def test_unrelated_query_scores_zero(self):
        assert core.score_match("dishwasher", STATES[0]) == 0

    def test_empty_query_scores_zero(self):
        assert core.score_match("", STATES[0]) == 0

    def test_phrase_matches_an_id_containing_neither_phrase_nor_space(self):
        # "bedside lamp" must find switch.bedside_lamp_socket_1 — a substring
        # test on the entity_id would not, and this is the normal spoken form.
        assert core.score_match("bedside lamp", STATES[2]) > 0

    def test_full_query_coverage_beats_partial(self):
        full = core.score_match("bedside lamp", STATES[2])
        part = core.score_match("bedside dishwasher", STATES[2])
        assert full > part


class TestFindEntities:
    def test_ranks_the_intended_entity_first(self):
        out = core.find_entities(STATES, "bedside lamp")
        assert out[0]["entity_id"] == "switch.bedside_lamp_socket_1"

    def test_domain_filter(self):
        out = core.find_entities(STATES, "", domain="scene", limit=50)
        assert {r["entity_id"] for r in out} == {"scene.main_lights_off",
                                                 "scene.movie_night"}

    def test_available_only_drops_unreachable(self):
        out = core.find_entities(STATES, "", domain="light", limit=50,
                                 available_only=True)
        assert [r["entity_id"] for r in out] == ["light.tcp_smart_bulb"]

    def test_no_match_returns_empty(self):
        assert core.find_entities(STATES, "dishwasher") == []

    def test_limit_is_honoured(self):
        assert len(core.find_entities(STATES, "", limit=3)) == 3

    def test_reports_availability(self):
        out = core.find_entities(STATES, "gaming")
        assert out[0]["available"] is False


class TestResolveEntity:
    def test_exact_entity_id(self):
        r = core.resolve_entity(STATES, "light.tcp_smart_bulb")
        assert r["entity_id"] == "light.tcp_smart_bulb"
        assert r["exact"] is True

    def test_unknown_entity_id_is_an_error_not_a_fuzzy_match(self):
        # Given something SHAPED like an entity_id, a near-miss must not be
        # silently substituted — that would switch the wrong device.
        r = core.resolve_entity(STATES, "light.tcp_smart_bulbs")
        assert "error" in r
        assert "no such entity" in r["error"]

    def test_phrase_resolves(self):
        r = core.resolve_entity(STATES, "bedside lamp")
        assert r["entity_id"] == "switch.bedside_lamp_socket_1"
        assert r["exact"] is False

    def test_empty_target(self):
        assert "error" in core.resolve_entity(STATES, "")

    def test_nothing_matches(self):
        assert "error" in core.resolve_entity(STATES, "dishwasher")

    def test_tie_is_refused_with_candidates(self):
        # Two sockets whose names differ only by a word the query omits: the
        # right answer is to ask, not to pick the first row and switch it.
        tied = [E("switch.lamp_socket_1", "on", "Lamp Socket 1"),
                E("switch.lamp_socket_2", "on", "Lamp Socket 2")]
        r = core.resolve_entity(tied, "lamp socket")
        assert "error" in r and "ambiguous" in r["error"]
        assert len(r["candidates"]) == 2


class TestSummarise:
    def test_counts_and_unavailable(self):
        s = core.summarise(STATES)
        assert s["total"] == len(STATES)
        assert s["domains"]["switch"] == 2
        # The three genuinely-unreachable devices, NOT the two idle scenes.
        assert s["unavailable_count"] == 3
        assert "light.gaming" in s["unavailable"]
        assert "scene.movie_night" not in s["unavailable"]

    def test_empty(self):
        assert core.summarise([])["total"] == 0


# ─────────────────────────────────────────────────────────────────────────────
#  Service calls
# ─────────────────────────────────────────────────────────────────────────────

class TestServiceForAction:
    def test_generic_domains(self):
        assert core.service_for_action("light", "on") == "turn_on"
        assert core.service_for_action("switch", "off") == "turn_off"
        assert core.service_for_action("light", "toggle") == "toggle"

    def test_a_scene_has_no_off(self):
        # Activating is the only thing a scene does; HA has no scene.turn_off.
        assert core.service_for_action("scene", "off") == "turn_on"

    def test_cover_uses_its_own_verbs(self):
        assert core.service_for_action("cover", "on") == "open_cover"
        assert core.service_for_action("cover", "off") == "close_cover"

    def test_bad_action_raises(self):
        with pytest.raises(ValueError):
            core.service_for_action("light", "explode")


class TestBuildServiceCall:
    def test_action_form(self):
        c = core.build_service_call("light.kitchen", action="on")
        assert c["domain"] == "light"
        assert c["service"] == "turn_on"
        assert c["payload"] == {"entity_id": "light.kitchen"}
        assert c["risky"] is False

    def test_explicit_service_bare(self):
        c = core.build_service_call("light.kitchen", service="turn_off")
        assert (c["domain"], c["service"]) == ("light", "turn_off")

    def test_explicit_service_dotted(self):
        c = core.build_service_call("light.kitchen", service="homeassistant.turn_off")
        assert (c["domain"], c["service"]) == ("homeassistant", "turn_off")

    def test_extra_data_passes_through(self):
        c = core.build_service_call("light.kitchen", action="on",
                                    data={"brightness_pct": 40})
        assert c["payload"]["brightness_pct"] == 40

    def test_data_cannot_override_the_target(self):
        c = core.build_service_call("light.kitchen", action="on",
                                    data={"entity_id": "light.elsewhere"})
        assert c["payload"]["entity_id"] == "light.kitchen"

    def test_none_values_are_dropped(self):
        c = core.build_service_call("light.kitchen", action="on",
                                    data={"brightness_pct": None})
        assert "brightness_pct" not in c["payload"]

    def test_needs_action_or_service(self):
        with pytest.raises(ValueError):
            core.build_service_call("light.kitchen")

    def test_rejects_a_non_entity_id(self):
        with pytest.raises(ValueError):
            core.build_service_call("kitchen", action="on")


class TestRisky:
    def test_unlocking_is_guarded(self):
        assert core.is_risky("lock", "unlock") is True
        assert core.build_service_call("lock.front", service="unlock")["risky"] is True

    def test_disarming_is_guarded(self):
        assert core.is_risky("alarm_control_panel", "alarm_disarm") is True

    def test_locking_is_not_guarded(self):
        # Only the direction that reduces security needs a confirmation.
        assert core.is_risky("lock", "lock") is False

    def test_a_light_is_not_guarded(self):
        assert core.is_risky("light", "turn_on") is False

    def test_case_insensitive(self):
        assert core.is_risky("LOCK", "UNLOCK") is True


class TestChangedEntityIds:
    def test_reads_the_changed_list(self):
        assert core.changed_entity_ids(
            [{"entity_id": "light.a"}, {"entity_id": "light.b"}]) == \
            ["light.a", "light.b"]

    def test_empty_is_normal_not_a_failure(self):
        # HA answers [] for a call that changed nothing (a light already on).
        assert core.changed_entity_ids([]) == []

    def test_non_list_is_tolerated(self):
        assert core.changed_entity_ids({"error": "x"}) == []
        assert core.changed_entity_ids(None) == []


# ─────────────────────────────────────────────────────────────────────────────
#  Notifications
# ─────────────────────────────────────────────────────────────────────────────

class TestNotify:
    @pytest.mark.parametrize("given", ["notify.smasnug", "smasnug"])
    def test_spellings_reach_the_same_service(self, given):
        assert core.notify_service(given) == ("notify", "smasnug")

    def test_other_domain_is_preserved(self):
        assert core.notify_service("persistent_notification.create") == \
            ("persistent_notification", "create")

    def test_empty_target(self):
        assert core.notify_service("") == ("notify", "notify")

    def test_payload_minimal(self):
        assert core.notify_payload("hello") == {"message": "hello"}

    def test_payload_with_title_and_data(self):
        p = core.notify_payload("hello", "Vera", {"ttl": 0})
        assert p == {"message": "hello", "title": "Vera", "data": {"ttl": 0}}
