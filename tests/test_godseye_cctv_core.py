"""CCTV source registry + feed parsers (pure — no network, no orchestrator).

The fixtures are trimmed from the REAL payloads captured 2026-08-30. That
matters more than usual here: the first draft of both parsers was written from
a plausible guess at the shape and would have returned zero cameras from every
provider while looking perfectly healthy. A parser that silently yields nothing
is the failure mode this file exists to catch.
"""
import pytest

from vera.godseye import godseye_cctv_core as C

# Real Caltrans lines. The field separator is a non-ASCII byte, and the still
# image url is NOT in the payload — it is derived from the detail page path.
CALTRANS = (
    "// developers looking for data please go here -> https://cwwp2.dot.ca.gov/\n"
    "var cctv = new Array();\n"
    "cctv[1] = 'https://cwwp2.dot.ca.gov/vm/loc/d1/sr20atsr1lookingeast.htm"
    "\xa4-123.807828\xa439.420010\xa4SR-20 : At SR-1 - Looking East (C020)\xa40';\n"
    "cctv[2] = 'https://cwwp2.dot.ca.gov/vm/loc/d1/sr20westofus101lookingeast.htm"
    "\xa4-123.369057\xa439.405805\xa4SR-20 : West Of US-101 - Looking East\xa41';\n"
)

# Real 511 record: the url lives in Views[], not at the top level.
ONENET = [{
    "Id": 1, "Latitude": 42.9142736713825, "Longitude": -78.9580061508579,
    "Roadway": "QEW", "Location": "QEW West of Thompson Road",
    "Views": [
        {"Id": 1, "Url": "https://511on.ca/map/Cctv/1", "Status": "Disabled"},
        {"Id": 2, "Url": "/map/Cctv/2", "Status": "Enabled"},
    ],
}]

TFL = [{
    "id": "JamCams_00002.00865", "commonName": "A406 Billet Upass E",
    "lat": 51.6, "lon": -0.02,
    "additionalProperties": [
        {"key": "available", "value": "true"},
        {"key": "imageUrl", "value": "https://s3.eu-west-1.amazonaws.com/jamcams/x.jpg"},
        {"key": "videoUrl", "value": "https://s3.eu-west-1.amazonaws.com/jamcams/x.mp4"},
    ],
}]

CALTRANS_SRC = {"id": "caltrans-d01", "kind": "caltrans", "provider": "Caltrans",
                "region": "California D01", "refresh_seconds": 300}
ONENET_SRC = {"id": "on-511", "kind": "onenetwork", "provider": "Ontario 511",
              "region": "Ontario", "base": "https://511on.ca", "refresh_seconds": 300}
TFL_SRC = {"id": "tfl-jamcams", "kind": "tfl", "provider": "TfL JamCams",
           "region": "London", "refresh_seconds": 5}


# ── the parsers must actually return cameras ─────────────────────────────────
def test_caltrans_parses_its_javascript_catalogue():
    feeds = C.parse_caltrans(CALTRANS, CALTRANS_SRC)
    assert len(feeds) == 2
    f = feeds[0]
    assert (f["lat"], f["lng"]) == (39.420010, -123.807828)   # lat/lng not swapped
    assert f["name"].startswith("SR-20")
    # The still image is derived from the detail page path by convention, which
    # is what makes twelve districts viable without a fetch per camera.
    assert f["url"] == ("https://cwwp2.dot.ca.gov/data/d1/cctv/image/"
                        "sr20atsr1lookingeast/sr20atsr1lookingeast.jpg")
    assert f["provider"] == "Caltrans"


def test_caltrans_ignores_noise_and_dedupes():
    assert C.parse_caltrans("var cctv = new Array();\n// nothing\n", CALTRANS_SRC) == []
    assert C.parse_caltrans("", CALTRANS_SRC) == []
    # Same camera listed twice collapses.
    assert len(C.parse_caltrans(CALTRANS + CALTRANS, CALTRANS_SRC)) == 2


def test_split_handles_the_non_ascii_separator():
    assert C.split_catalog_payload("a\xa4b\xa4c") == ["a", "b", "c"]
    assert C.split_catalog_payload("") == []


def test_511_reads_the_enabled_view_not_a_top_level_url():
    feeds = C.parse_onenetwork(ONENET, ONENET_SRC)
    assert len(feeds) == 1
    # Disabled view skipped; relative url resolved against the provider base.
    assert feeds[0]["url"] == "https://511on.ca/map/Cctv/2"
    assert feeds[0]["lat"] == pytest.approx(42.91427, rel=1e-4)


def test_511_camera_with_no_enabled_view_is_dropped():
    rows = [{**ONENET[0], "Views": [{"Url": "/x", "Status": "Disabled"}]}]
    assert C.parse_onenetwork(rows, ONENET_SRC) == []
    assert C.parse_onenetwork([{"Views": [{"Url": "/x", "Status": "Enabled"}]}],
                              ONENET_SRC) == []          # no coordinates


def test_tfl_reads_the_additional_properties_bag():
    feeds = C.parse_tfl(TFL, TFL_SRC)
    assert len(feeds) == 1
    f = feeds[0]
    assert f["videoUrl"].endswith(".mp4") and f["fallbackUrl"].endswith(".jpg")
    # An mp4 clip is "video", never "stream" — TfL has no live stream, and
    # calling it one is what would make the looping look like a bug.
    assert f["mediaType"] == "video"
    assert f["city"] == "London"


@pytest.mark.parametrize("payload", [None, [], {}, "junk", [None], [{"lat": "x"}]])
def test_no_parser_explodes_on_a_changed_payload(payload):
    for src, fn in ((CALTRANS_SRC, C.parse_caltrans),
                    (ONENET_SRC, C.parse_onenetwork), (TFL_SRC, C.parse_tfl)):
        if fn is C.parse_caltrans and not isinstance(payload, str):
            continue
        assert fn(payload, src) == []


# ── registry ─────────────────────────────────────────────────────────────────
def test_every_registered_source_has_a_parser_and_is_unique():
    ids = [s["id"] for s in C.SOURCES]
    assert len(ids) == len(set(ids))
    for s in C.SOURCES:
        assert s["kind"] in C.PARSERS, s["id"]
        assert s["url"].startswith("https://")


def test_all_twelve_caltrans_districts_are_registered():
    # Godseye reads district 08 only; the other eleven are real, distinct data
    # (verified by hash) and are pure additional coverage.
    districts = {s["id"] for s in C.SOURCES if s["kind"] == "caltrans"}
    assert len(districts) == 12


def test_providers_known_to_require_a_key_are_not_registered():
    # Alberta/Florida/Georgia answer "Invalid Key" and NJ/Utah 403 as of
    # 2026-08-30. Godseye still calls Alberta; we must not repeat that.
    registered = {s["url"] for s in C.SOURCES}
    for dead in C.CLOSED_SOURCES:
        assert dead not in registered
    assert any("alberta" in u for u in C.CLOSED_SOURCES)


# ── manifest ─────────────────────────────────────────────────────────────────
def test_manifest_is_the_shape_godseye_parses():
    groups = [C.parse_caltrans(CALTRANS, CALTRANS_SRC),
              C.parse_onenetwork(ONENET, ONENET_SRC), C.parse_tfl(TFL, TFL_SRC)]
    m = C.build_manifest(groups, generated_at="2026-08-30T00:00:00Z")
    assert m["feedCount"] == 4 and len(m["feeds"]) == 4
    assert m["byProvider"]["Caltrans"] == 2
    for f in m["feeds"]:
        # mergeFeeds() drops anything without finite lat/lng — note lng, not lon.
        assert isinstance(f["lat"], float) and isinstance(f["lng"], float)
        assert set(f) == set(C.FEED_KEYS)


def test_merge_dedupes_and_drops_uncoordinated_feeds():
    a = C.parse_tfl(TFL, TFL_SRC)
    assert len(C.merge_feeds([a, a])) == 1
    assert C.merge_feeds([[{"provider": "x", "id": "1"}]]) == []
    assert C.merge_feeds([[{"lat": "no", "lng": 1, "id": "1"}]]) == []


def test_manifest_records_which_sources_failed():
    m = C.build_manifest([], generated_at="t", errors={"ia-511": "timeout"})
    # A provider going dark must be visible, not silently a smaller map.
    assert m["errors"]["ia-511"] == "timeout" and m["feedCount"] == 0
