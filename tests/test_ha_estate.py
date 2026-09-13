"""Tests for vera/homeassistant/ha_estate.py — the estate → HA projection.

The property that matters most here is negative: a sync must never be able to
update or delete an entity it did not create. Most of these tests exist to
hold that line rather than to check the happy path.

Imported lowercase with the worktree on sys.path — `Vera.vera.…` would resolve
to the main checkout and silently test code this branch never changed.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.homeassistant import ha_estate as estate  # noqa: E402


def I(id_, label, kind="generic", port=None, base_url=""):
    return {"id": id_, "label": label, "kind": kind, "port": port,
            "base_url": base_url or f"http://127.0.0.1:{port or 80}"}


def S(entity_id, source=None, **attrs):
    a = dict(attrs)
    if source:
        a["source"] = source
    return {"entity_id": entity_id, "state": "on", "attributes": a}


# Shaped like the real registry, duplicate labels included.
REGISTRY = [
    I("a", "jellyfin", "generic", 8096),
    I("b", "n8n", "n8n", 5678),
    I("c", "traefik", "generic", 80),
    I("d", "traefik", "generic", 443),
    I("e", "vera-dev-feat-something", "generic", 8991),
    I("f", "home-assistant", "homeassistant", 8123),
]


class TestSlugAndEphemeral:
    def test_slugify(self):
        assert estate.slugify("Home Assistant") == "home_assistant"
        assert estate.slugify("vera-dev-01") == "vera_dev_01"

    def test_slugify_fallback(self):
        assert estate.slugify("") == "service"
        assert estate.slugify("!!!") == "service"

    @pytest.mark.parametrize("label", [
        "vera-dev-feat-x", "sbxw-abc", "vera-sandbox-2", "a-worktree-thing"])
    def test_ephemeral_detected(self, label):
        assert estate.is_ephemeral(label) is True

    @pytest.mark.parametrize("label", ["jellyfin", "n8n", "traefik", "gitea"])
    def test_real_services_are_not_ephemeral(self, label):
        assert estate.is_ephemeral(label) is False


class TestEntityIds:
    def test_unique_label_keeps_the_short_form(self):
        ids = estate.entity_ids_for(REGISTRY)
        assert ids["a"] == "binary_sensor.estate_jellyfin"

    def test_duplicate_label_is_disambiguated_by_port(self):
        # Three traefik rows exist in the real registry, one per published
        # port; without the port they would collide onto one entity and the
        # last write would win silently.
        ids = estate.entity_ids_for(REGISTRY)
        assert ids["c"] == "binary_sensor.estate_traefik_80"
        assert ids["d"] == "binary_sensor.estate_traefik_443"
        assert ids["c"] != ids["d"]

    def test_ids_are_stable_across_calls(self):
        assert estate.entity_ids_for(REGISTRY) == estate.entity_ids_for(REGISTRY)

    def test_rows_without_an_id_are_dropped(self):
        assert estate.entity_ids_for([{"label": "x"}]) == {}


class TestBuildEntity:
    def test_carries_the_ownership_tag(self):
        e = estate.build_entity(REGISTRY[0], "binary_sensor.estate_jellyfin", True)
        assert e["attributes"]["source"] == estate.SOURCE_TAG

    def test_connectivity_device_class(self):
        e = estate.build_entity(REGISTRY[0], "binary_sensor.estate_jellyfin", True)
        assert e["attributes"]["device_class"] == "connectivity"

    def test_reachable_maps_to_on_and_off(self):
        up = estate.build_entity(REGISTRY[0], "x", True)
        down = estate.build_entity(REGISTRY[0], "x", False)
        assert (up["state"], down["state"]) == ("on", "off")

    def test_unprobed_is_unknown_not_off(self):
        # Claiming a service is DOWN because nobody looked would fire any
        # automation built on it.
        e = estate.build_entity(REGISTRY[0], "x", None)
        assert e["state"] == "unknown"

    def test_carries_connection_detail(self):
        e = estate.build_entity(REGISTRY[1], "x", True)
        assert e["attributes"]["port"] == 5678
        assert e["attributes"]["kind"] == "n8n"
        assert e["attributes"]["base_url"] == "http://127.0.0.1:5678"

    def test_empty_fields_are_omitted(self):
        e = estate.build_entity({"id": "z", "label": "z"}, "x", True)
        assert "port" not in e["attributes"]


class TestOwnedEntityIds:
    def test_only_tagged_entities_are_ours(self):
        states = [
            S("binary_sensor.estate_jellyfin", source=estate.SOURCE_TAG),
            S("light.kitchen"),
            S("switch.bedside_lamp_socket_1"),
        ]
        assert estate.owned_entity_ids(states) == ["binary_sensor.estate_jellyfin"]

    def test_a_lookalike_name_is_not_ours(self):
        # Ownership is the tag we stamped, never the name — a user is free to
        # call something estate_anything and it must stay untouched.
        states = [S("binary_sensor.estate_hand_made")]
        assert estate.owned_entity_ids(states) == []

    def test_another_tools_tag_is_not_ours(self):
        states = [S("binary_sensor.estate_x", source="some-other-tool")]
        assert estate.owned_entity_ids(states) == []

    def test_empty(self):
        assert estate.owned_entity_ids([]) == []


class TestPlanSync:
    def test_ephemeral_sandboxes_are_skipped_by_default(self):
        plan = estate.plan_sync(REGISTRY, [])
        created = {r["entity_id"] for r in plan["create"]}
        assert not any("vera_dev" in e for e in created)
        assert plan["counts"]["skipped"] == 1

    def test_include_ephemeral_opts_them_back_in(self):
        plan = estate.plan_sync(REGISTRY, [], include_ephemeral=True)
        created = {r["entity_id"] for r in plan["create"]}
        assert any("vera_dev" in e for e in created)
        assert plan["counts"]["skipped"] == 0

    def test_first_run_is_all_creates(self):
        plan = estate.plan_sync(REGISTRY, [])
        assert plan["counts"]["create"] == 5
        assert plan["counts"]["update"] == 0

    def test_second_run_is_all_updates(self):
        first = estate.plan_sync(REGISTRY, [])
        existing = [S(r["entity_id"], source=estate.SOURCE_TAG)
                    for r in first["create"]]
        second = estate.plan_sync(REGISTRY, existing)
        assert second["counts"]["create"] == 0
        assert second["counts"]["update"] == 5
        assert second["counts"]["remove"] == 0

    def test_a_service_that_vanished_is_removed(self):
        existing = [S("binary_sensor.estate_gone", source=estate.SOURCE_TAG)]
        plan = estate.plan_sync(REGISTRY, existing)
        assert [r["entity_id"] for r in plan["remove"]] == \
            ["binary_sensor.estate_gone"]

    def test_prune_off_removes_nothing(self):
        existing = [S("binary_sensor.estate_gone", source=estate.SOURCE_TAG)]
        plan = estate.plan_sync(REGISTRY, existing, prune=False)
        assert plan["remove"] == []

    def test_a_real_device_is_never_in_the_remove_list(self):
        # The single most important property: the user's actual lights and
        # switches are not tagged, so a prune cannot reach them however
        # little the registry contains.
        house = [S("light.tcp_smart_bulb"), S("switch.bedside_lamp_socket_1"),
                 S("scene.movie_night"), S("person.joe_baker")]
        plan = estate.plan_sync([], house)
        assert plan["remove"] == []

    def test_a_real_device_is_untouched_even_when_it_looks_like_ours(self):
        house = [S("binary_sensor.estate_jellyfin")]  # same name, no tag
        plan = estate.plan_sync(REGISTRY, house)
        assert plan["remove"] == []
        # and it is planned as a CREATE, not an update, because we do not own it
        assert "binary_sensor.estate_jellyfin" in \
            {r["entity_id"] for r in plan["create"]}

    def test_empty_registry_with_no_owned_entities_is_a_no_op(self):
        plan = estate.plan_sync([], [])
        assert plan["counts"] == {"create": 0, "update": 0, "remove": 0,
                                  "skipped": 0}

    def test_counts_match_the_lists(self):
        plan = estate.plan_sync(REGISTRY, [])
        for k in ("create", "update", "remove", "skipped"):
            assert plan["counts"][k] == len(plan[k])


class TestProbeTargets:
    def test_pairs_id_with_url(self):
        assert ("b", "http://127.0.0.1:5678") in estate.probe_targets(REGISTRY)

    def test_rows_without_a_url_are_skipped(self):
        assert estate.probe_targets([{"id": "x", "label": "x", "base_url": ""}]) == []


class TestSummarise:
    def test_reads_as_a_sentence(self):
        s = estate.summarise_plan(estate.plan_sync(REGISTRY, []))
        assert "5 to add" in s and "1 skipped" in s
