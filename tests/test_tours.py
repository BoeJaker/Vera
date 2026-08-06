"""Tours: mini-DSL parsing, step validation, ref matching (no browser)."""

from vera.operator import perception as P
from vera.operator import tours as T


def test_parse_steps_mini_dsl():
    steps = T.parse_steps(
        'wait 1200; click_text Run backtest; gif_start 700; gif_stop equity 800; '
        'shot done; type Goal "hi there"; scroll 500')
    kinds = [s["do"] for s in steps]
    assert kinds == ["wait", "click_text", "gif_start", "gif_stop", "shot",
                     "type_text", "scroll"]
    assert steps[0]["ms"] == 1200
    assert steps[1]["text"] == "Run backtest"
    assert steps[3]["name"] == "equity" and steps[3]["duration_ms"] == 800
    assert steps[5]["text_target"] == "Goal" and steps[5]["text"] == "hi there"
    assert steps[6]["dy"] == 500


def test_validate_step():
    assert T.validate_step({"do": "wait", "ms": 1})["ok"]
    assert not T.validate_step({"do": "nope"})["ok"]
    assert not T.validate_step({"do": "goto"})["ok"]
    assert not T.validate_step({"do": "click_text", "text": ""})["ok"]
    assert not T.validate_step({"do": "seed"})["ok"]


def test_find_ref_by_name_then_role():
    obs = P.build_observation({"elements": [
        {"ref": "e1", "role": "button", "name": "Run backtest"},
        {"ref": "e2", "role": "textbox", "name": "Goal"}]})
    assert T.find_ref(obs, "backtest") == "e1"
    assert T.find_ref(obs, "goal") == "e2"
    assert T.find_ref(obs, "textbox") == "e2"   # falls back to role
    assert T.find_ref(obs, "nomatch") == ""


def test_example_tours_are_valid():
    assert T.list_tours()
    for slug in T.list_tours():
        tour = T.get_tour(slug)
        assert tour["steps"], slug
        for s in tour["steps"]:
            assert T.validate_step(s)["ok"], (slug, s)
