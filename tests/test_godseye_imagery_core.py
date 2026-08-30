"""Geospatial image search — sources and parsers (pure, no network).

Fixtures are trimmed from REAL responses captured 2026-08-30. That is the point
of this file: in the CCTV work earlier the same day, two parsers written from a
plausible guess at the payload would have returned zero results from every
provider while reporting themselves perfectly healthy.
"""
import math

import pytest

from vera.godseye import godseye_imagery_core as C

# Commons geosearch carries coordinates but NO urls, and returns audio files.
COMMONS_GEO = {"batchcomplete": "", "query": {"geosearch": [
    {"pageid": 176884078, "ns": 6, "title": "File:Boris-grey-interview.ogg",
     "lat": 51.5074, "lon": -0.12779999999998},
    {"pageid": 155024564, "ns": 6, "title": "File:Map of Punt region.jpg",
     "lat": 51.5074, "lon": -0.1278},
]}}

COMMONS_INFO = {"query": {"pages": {
    "176884078": {"pageid": 176884078, "title": "File:Boris-grey-interview.ogg",
                  "imageinfo": [{"url": "https://upload.wikimedia.org/x.ogg",
                                 "mime": "application/ogg"}]},
    "155024564": {"pageid": 155024564, "title": "File:Map of Punt region.jpg",
                  "imageinfo": [{
                      "thumburl": "https://upload.wikimedia.org/thumb/330px-Map.jpg",
                      "url": "https://upload.wikimedia.org/Map_of_Punt_region.jpg",
                      "mime": "image/jpeg",
                      "extmetadata": {
                          "LicenseShortName": {"value": "CC BY-SA 4.0"},
                          "Artist": {"value": "<a href='#'>Some Author</a>"},
                          "DateTimeOriginal": {"value": "2019-04-02"}}}]},
}}}

WIKIPEDIA = {"query": {"pages": {
    "424305": {"pageid": 424305, "title": "Central London",
               "thumbnail": {"source": "https://upload.wikimedia.org/330px-london.png"},
               "coordinates": [{"lat": 51.5075, "lon": -0.1275, "globe": "earth"}]},
    "999999": {"pageid": 999999, "title": "No Image Here",
               "coordinates": [{"lat": 51.5, "lon": -0.1}]},
}}}

INAT = {"total_results": 35494, "results": [{
    "id": 395753347, "uri": "https://www.inaturalist.org/observations/395753347",
    "location": "51.5038416667,-0.1201",           # a STRING, not two numbers
    "observed_on": "2026-08-30", "species_guess": "Blackbird",
    "user": {"login": "someone"},
    "photos": [{"url": "https://inaturalist-open-data.s3.amazonaws.com/photos/1/square.jpg",
                "license_code": "cc-by-nc"}],
}]}

MAPILLARY = {"data": [{
    "id": "123", "thumb_1024_url": "https://images.mapillary.com/123/thumb-1024.jpg",
    "captured_at": 1690000000000, "compass_angle": 217.5,
    "computed_geometry": {"type": "Point", "coordinates": [-0.1278, 51.5074]},
}]}

FLICKR = {"photos": {"photo": [{
    "id": "5551", "owner": "42@N00", "ownername": "Photographer",
    "title": "Trafalgar Square", "latitude": "51.508", "longitude": "-0.128",
    "url_s": "https://live.staticflickr.com/5551_s.jpg",
    "url_m": "https://live.staticflickr.com/5551_m.jpg",
    "datetaken": "2021-06-04 11:02:33", "license": "4",
}]}}


# ── Commons: the two-call join, and the file-vs-image trap ───────────────────
def test_commons_joins_geosearch_to_imageinfo():
    recs = C.parse_commons(COMMONS_GEO, COMMONS_INFO)
    # The .ogg is dropped: geosearch is a FILE search, not an image search.
    assert len(recs) == 1
    r = recs[0]
    assert r["title"] == "Map of Punt region"          # "File:" prefix stripped
    assert r["thumbUrl"].endswith("330px-Map.jpg")
    assert r["license"] == "CC BY-SA 4.0"
    assert r["attribution"] == "Some Author"           # html stripped
    assert r["capturedAt"] == "2019-04-02"
    assert (r["lat"], round(r["lng"], 4)) == (51.5074, -0.1278)


def test_commons_needs_both_halves():
    assert C.parse_commons(COMMONS_GEO, {}) == []      # urls missing
    assert C.parse_commons({}, COMMONS_INFO) == []     # coordinates missing
    assert C.parse_commons(None, None) == []


# ── Wikipedia: one call, but articles without images are not imagery ─────────
def test_wikipedia_keeps_only_pages_with_a_thumbnail():
    recs = C.parse_wikipedia(WIKIPEDIA)
    assert len(recs) == 1 and recs[0]["title"] == "Central London"
    assert recs[0]["pageUrl"].endswith("curid=424305")


# ── iNaturalist: the location string ─────────────────────────────────────────
def test_inaturalist_parses_the_location_string_and_upsizes_the_photo():
    recs = C.parse_inaturalist(INAT)
    assert len(recs) == 1
    r = recs[0]
    assert r["lat"] == pytest.approx(51.50384, rel=1e-5)
    assert r["lng"] == pytest.approx(-0.1201, rel=1e-4)
    assert r["thumbUrl"].endswith("/square.jpg")
    assert r["fullUrl"].endswith("/large.jpg")         # same asset, usable size
    assert r["license"] == "cc-by-nc" and r["capturedAt"] == "2026-08-30"


def test_inaturalist_drops_observations_without_photo_or_place():
    assert C.parse_inaturalist({"results": [{"id": 1, "location": "1,2"}]}) == []
    assert C.parse_inaturalist({"results": [{"id": 1, "photos": [{"url": "u"}]}]}) == []


# ── Mapillary: GeoJSON order, and the bearing we care about ──────────────────
def test_mapillary_reads_lon_lat_order_and_keeps_the_bearing():
    r = C.parse_mapillary(MAPILLARY)[0]
    # GeoJSON is [lon, lat]; reading it as lat/lng puts London in the ocean.
    assert (round(r["lat"], 4), round(r["lng"], 4)) == (51.5074, -0.1278)
    # Pose is the reason this provider earns its place — later work needs it.
    assert r["bearing"] == 217.5


def test_flickr_parses_geo_extras():
    r = C.parse_flickr(FLICKR)[0]
    assert (round(r["lat"], 3), round(r["lng"], 3)) == (51.508, -0.128)
    assert r["capturedAt"].startswith("2021-06-04")
    assert "42@N00" in r["pageUrl"]


@pytest.mark.parametrize("payload", [None, {}, [], "junk", {"data": None},
                                     {"results": [None]}, {"photos": {}}])
def test_no_parser_raises_on_a_changed_payload(payload):
    # A provider serving an error page must yield nothing, not take the whole
    # fan-out down with it.
    for fn in (C.parse_wikipedia, C.parse_inaturalist, C.parse_mapillary,
               C.parse_flickr):
        assert fn(payload) == []


# ── bbox reduction ───────────────────────────────────────────────────────────
def test_radius_contains_the_box_rather_than_fitting_inside_it():
    lat, lon, radius = C.bbox_centre((51.49, -0.15, 51.52, -0.10))
    assert lat == pytest.approx(51.505) and lon == pytest.approx(-0.125)
    # Half-diagonal, so the circle covers the corners; an inscribed circle
    # would silently drop results near them.
    half_lat_m = 0.03 * 111_320.0 / 2.0
    assert radius > half_lat_m


def test_radius_is_clamped_to_what_mediawiki_accepts():
    # gsradius above 10km is rejected outright by the API.
    assert C.clamp_radius(50_000) == 10_000
    assert C.clamp_radius(0) == 10
    _, _, r = C.bbox_centre((-10.0, -10.0, 10.0, 10.0))
    assert C.clamp_radius(r) == 10_000


def test_bbox_centre_survives_the_poles():
    # cos(lat) -> 0 at the pole; without a guard the radius collapses to zero
    # and the search returns nothing.
    _, _, r = C.bbox_centre((89.0, 0.0, 90.0, 10.0))
    assert r > 0 and math.isfinite(r)


# ── request builders ─────────────────────────────────────────────────────────
def test_commons_params_ask_for_files_only_and_batch_ids():
    p = C.commons_geosearch_params(51.5, -0.1, 1000, limit=9)
    assert p["gsnamespace"] == "6" and p["gscoord"] == "51.5|-0.1"
    assert C.commons_imageinfo_params([1, 2, 3])["pageids"] == "1|2|3"


def test_wikipedia_params_fetch_coords_and_thumbs_in_one_call():
    p = C.wikipedia_geosearch_params(51.5, -0.1, 1000)
    assert p["generator"] == "geosearch"
    assert "pageimages" in p["prop"] and "coordinates" in p["prop"]


def test_mapillary_and_flickr_use_lon_lat_bbox_order():
    bbox = (51.49, -0.15, 51.52, -0.10)               # s, w, n, e
    assert C.mapillary_params(bbox, "tok")["bbox"] == "-0.15,51.49,-0.1,51.52"
    assert C.flickr_params(bbox, "k")["bbox"] == "-0.15,51.49,-0.1,51.52"


# ── assembly ─────────────────────────────────────────────────────────────────
def test_result_separates_a_broken_provider_from_a_skipped_one():
    res = C.build_result([C.parse_wikipedia(WIKIPEDIA), C.parse_inaturalist(INAT)],
                         bbox=(1, 2, 3, 4), generated_at="t",
                         errors={"flickr": "HTTP 500"},
                         skipped={"mapillary": "no VERA_MAPILLARY_TOKEN"})
    assert res["count"] == 2
    assert set(res["byProvider"]) == {"Wikipedia", "iNaturalist"}
    # Collapsing these two would hide a broken source behind "no key".
    assert res["errors"]["flickr"] and res["skipped"]["mapillary"]


def test_dedupe_collapses_repeats_and_drops_placeless_records():
    a = C.parse_inaturalist(INAT)
    assert len(C.dedupe(a + a)) == 1
    assert C.dedupe([{"id": "x"}]) == []               # no coordinates


def test_every_open_provider_has_a_parser():
    for name in C.OPEN_PROVIDERS:
        assert hasattr(C, f"parse_{name}")
    for name in C.KEYED_PROVIDERS:
        assert hasattr(C, f"parse_{name}")
