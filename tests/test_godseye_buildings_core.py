"""OSM building footprints (pure — no network).

Fixture shape captured live from Overpass on 2026-08-30: `out tags geom`
returns `geometry: [{lat, lon}, ...]` inline alongside `tags`.
"""
import pytest

from vera.godseye import godseye_buildings_core as C

SQUARE = [{"lat": 51.5069, "lon": -0.1279}, {"lat": 51.5070, "lon": -0.1279},
          {"lat": 51.5070, "lon": -0.1277}, {"lat": 51.5069, "lon": -0.1277}]

PAYLOAD = {"version": 0.6, "elements": [
    {"type": "way", "id": 107945685, "geometry": SQUARE,
     "tags": {"building": "yes", "height": "23.5", "name": "Tall Thing"}},
    {"type": "way", "id": 2, "geometry": SQUARE,
     "tags": {"building": "yes", "building:levels": "4"}},
    {"type": "relation", "id": 3, "geometry": SQUARE, "tags": {"building": "yes"}},
]}


# ── heights: OSM tags are free text ──────────────────────────────────────────
@pytest.mark.parametrize("tags,expected", [
    ({"height": "23.5"}, 23.5),
    ({"height": "12 m"}, 12.0),          # units tacked on
    ({"height": "40'"}, 40.0),
    ({"building:height": "9"}, 9.0),
    ({"building:levels": "4"}, 4 * C.LEVEL_HEIGHT_M),
    ({"levels": "2"}, 2 * C.LEVEL_HEIGHT_M),
    ({"height": "yes"}, C.DEFAULT_HEIGHT_M),      # not a number at all
    ({"height": "0"}, C.DEFAULT_HEIGHT_M),        # zero reads as missing data
    ({"height": "-5"}, C.DEFAULT_HEIGHT_M),
    ({}, C.DEFAULT_HEIGHT_M),
    (None, C.DEFAULT_HEIGHT_M),
])
def test_height_falls_back_rather_than_extruding_nothing(tags, expected):
    # A zero-height building is indistinguishable from a missing one on screen.
    assert C.parse_height(tags) == expected


def test_height_prefers_metres_over_levels():
    assert C.parse_height({"height": "30", "building:levels": "2"}) == 30.0


@pytest.mark.parametrize("tags,expected", [
    ({"min_height": "5"}, 5.0), ({"min_height": "junk"}, 0.0),
    ({"min_height": "-2"}, 0.0), ({}, 0.0),
])
def test_min_height_lifts_only_when_valid(tags, expected):
    assert C.parse_min_height(tags) == expected


# ── geometry ─────────────────────────────────────────────────────────────────
def test_parse_flattens_to_lon_lat_pairs_for_cesium():
    out = C.parse_overpass(PAYLOAD)
    assert len(out) == 3
    b = out[0]
    assert b["id"] == "way-107945685" and b["height"] == 23.5
    assert b["name"] == "Tall Thing"
    # Cesium's fromDegreesArray wants a flat lon,lat,lon,lat run; doing it here
    # avoids a per-vertex transform in the browser for thousands of shapes.
    assert b["coords"][:2] == [-0.1279, 51.5069]
    assert len(b["coords"]) == len(SQUARE) * 2


def test_relations_are_kept():
    # Courtyard and large buildings are multipolygons; dropping relations
    # silently loses exactly the landmarks people look for.
    assert any(b["id"].startswith("relation-") for b in C.parse_overpass(PAYLOAD))


@pytest.mark.parametrize("payload", [
    None, {}, "junk", [], {"elements": "nope"}, {"elements": [None]},
    {"elements": [{"geometry": []}]},
    {"elements": [{"geometry": [{"lat": 1, "lon": 2}]}]},          # too few points
    {"elements": [{"geometry": [{"lat": "x", "lon": "y"}] * 4}]},  # non-numeric
])
def test_a_changed_or_broken_payload_yields_nothing(payload):
    assert C.parse_overpass(payload) == []


def test_limit_is_honoured():
    big = {"elements": PAYLOAD["elements"] * 100}
    assert len(C.parse_overpass(big, limit=5)) == 5


# ── bounds: Overpass is shared infrastructure ────────────────────────────────
def test_a_sane_neighbourhood_bbox_is_accepted():
    ok, why = C.bbox_is_sane((51.500, -0.130, 51.510, -0.120))
    assert ok and why == ""


@pytest.mark.parametrize("bbox,fragment", [
    ((51.51, -0.13, 51.50, -0.12), "south < north"),   # inverted
    ((51.50, -0.12, 51.51, -0.13), "south < north"),   # west/east swapped
    ((-91, 0, 10, 1), "out of range"),
    ((0, 0, 45, 45), "too large"),                     # city-scale or worse
    (("a", 0, 1, 1), "four numbers"),
    ((0, 0, 0, 1), "south < north"),                   # zero height
])
def test_unreasonable_queries_are_refused_before_being_sent(bbox, fragment):
    ok, why = C.bbox_is_sane(bbox)
    assert ok is False and fragment in why


def test_query_covers_ways_and_relations_and_asks_for_inline_geometry():
    q = C.overpass_query((51.50, -0.13, 51.51, -0.12), timeout=25)
    assert '[out:json][timeout:25]' in q
    assert 'way["building"]' in q and 'relation["building"]' in q
    # `out tags geom` is what makes this ONE request instead of a second pass
    # resolving node ids.
    assert "out tags geom" in q
    assert "51.5,-0.13,51.51,-0.12" in q


# ── caching ──────────────────────────────────────────────────────────────────
def test_cache_key_snaps_so_a_small_pan_reuses_the_answer():
    a = C.bbox_cache_key((51.50001, -0.13001, 51.51001, -0.12001))
    b = C.bbox_cache_key((51.50002, -0.13002, 51.51002, -0.12002))
    assert a == b
    assert a != C.bbox_cache_key((51.60, -0.13, 51.61, -0.12))


def test_result_says_when_it_is_a_subset():
    # A dense centre truncated to the limit must not look like a sparse one.
    r = C.build_result(C.parse_overpass(PAYLOAD), bbox=(1, 2, 3, 4),
                       generated_at="t", truncated=True, cached=True)
    assert r["count"] == 3 and r["truncated"] is True and r["cached"] is True
    assert "OpenStreetMap" in r["attribution"]        # ODbL requires it
