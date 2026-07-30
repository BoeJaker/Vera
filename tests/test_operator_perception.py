"""Perception: Observation building, ref map, compact rendering (no browser)."""

from vera.operator import perception as P


def _scan():
    return {
        "url": "http://localhost:8998/x", "title": "Demo", "text": "hello world",
        "elements": [
            {"ref": "e1", "role": "button", "name": "Run", "bbox": [1, 2, 3, 4], "enabled": True},
            {"ref": "e2", "role": "textbox", "name": "Goal", "value": "", "enabled": True},
            {"bad": "no-ref"},  # dropped
        ],
    }


def test_build_observation_basic():
    obs = P.build_observation(_scan(), "shot.png")
    assert obs.url.endswith("/x")
    assert obs.title == "Demo"
    assert obs.screenshot_path == "shot.png"
    assert len(obs.elements) == 2  # the ref-less entry is dropped
    assert obs.elements[0].ref == "e1"
    assert obs.elements[0].role == "button"


def test_ref_map_selectors():
    obs = P.build_observation(_scan())
    rm = obs.ref_map()
    assert rm["e1"]["selector"] == '[data-vera-ref="e1"]'
    assert rm["e2"]["role"] == "textbox"


def test_element_one_line():
    obs = P.build_observation(_scan())
    assert obs.elements[0].one_line() == 'e1: button "Run"'


def test_compact_contains_goal_and_elements():
    obs = P.build_observation(_scan())
    txt = obs.compact()
    assert "INTERACTIVE ELEMENTS" in txt
    assert "e1: button" in txt
    assert "hello world" in txt


def test_build_observation_empty_is_safe():
    obs = P.build_observation({})
    assert obs.elements == []
    assert obs.to_dict()["element_count"] == 0
