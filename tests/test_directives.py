"""Doc-directive capture: parse, step-build, idempotent insert (no browser)."""

from vera.operator.docs import directives as D


def test_parse_attrs():
    a = D.parse_attrs('panel="markets" name=\'bt\' gif="true" flag')
    assert a["panel"] == "markets"
    assert a["name"] == "bt"
    assert a["gif"] == "true"
    assert a["flag"] is True


def test_parse_directives():
    md = ('x\n<!-- VERA:CAPTURE panel="markets" name="bt" -->\ny\n'
          '<!-- VERA:CAPTURE url="http://x" name="two" gif="true" -->\n')
    ds = D.parse_directives(md)
    assert len(ds) == 2
    assert ds[0]["attrs"]["name"] == "bt"
    assert ds[1]["attrs"]["gif"] == "true"


def test_directive_steps_default_shot():
    steps = D.directive_steps({"panel": "markets", "name": "bt"})
    assert steps[0]["do"] == "goto"
    assert steps[-1]["do"] == "shot" and steps[-1]["name"] == "bt"


def test_directive_steps_default_gif():
    steps = D.directive_steps({"panel": "markets", "name": "bt", "gif": "true"})
    assert any(s["do"] == "gif_start" for s in steps)
    assert steps[-1]["do"] == "gif_stop"


def test_directive_steps_custom():
    steps = D.directive_steps({"panel": "m", "name": "bt", "steps": "click_text Run; shot bt"})
    kinds = [s["do"] for s in steps]
    assert "click_text" in kinds and kinds[-1] == "shot"


def test_upsert_capture_insert_preserves_prose():
    md = '# T\n\nprose above\n\n<!-- VERA:CAPTURE panel="m" name="bt" -->\n\nprose below\n'
    d = D.parse_directives(md)[0]
    img = D.image_markdown("bt", "assets/m/bt.png")
    out = D.upsert_capture(md, "bt", img, after_pos=d["end"])
    assert "VERA:CAPTURED bt" in out
    assert "prose above" in out and "prose below" in out
    assert out.count("<!-- VERA:CAPTURED bt -->") == 1


def test_upsert_capture_idempotent():
    md = '<!-- VERA:CAPTURE panel="m" name="bt" -->\n'
    img = D.image_markdown("bt", "assets/m/bt.gif", gif=True)
    once = D.upsert_capture(md, "bt", img, after_pos=len(md))
    twice = D.upsert_capture(once, "bt", img)
    assert twice.count("<!-- VERA:CAPTURED bt -->") == 1  # replaced, not duplicated
