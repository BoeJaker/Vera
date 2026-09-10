"""
The style packs, density tiers, data-viz ramp and derived surfaces the UI
redesign adds to the theme system (Notes/40 §1). theme_defs is dependency-free,
so this runs anywhere.
"""
from Vera.vera.theme_defs import (
    BUILTIN_THEMES, STYLE_PACKS, DEFAULT_STYLE, DENSITY_TIERS, DEFAULT_DENSITY,
    DATAVIZ_RAMP, DERIVED_VARS, appearance_css, style_pack_css, with_dataviz,
    normalize_appearance,
)

PACK_KEYS = {
    "--f-ui", "--f-prose", "--f-disp", "--f-mono", "--ui-radius", "--r-sm", "--r-pill",
    "--row-h", "--pad", "--label-size", "--label-case", "--label-track", "--label-weight",
    "--body", "--lead", "--fill", "--pri-bg", "--pri-fg", "--pri-bd", "--card", "--elev", "--elev-lo",
}


def test_four_packs_carry_the_whole_contract():
    assert set(STYLE_PACKS) == {"standard", "newspaper", "terminal", "pixel"}
    assert DEFAULT_STYLE in STYLE_PACKS
    for pid, pack in STYLE_PACKS.items():
        missing = PACK_KEYS - set(pack["vars"])
        assert not missing, f"{pid} is missing {sorted(missing)}"
        assert pack["label"]


def test_tiers_and_defaults():
    assert list(DENSITY_TIERS) == ["full", "hover", "zen"]
    assert DEFAULT_DENSITY == "full"


def test_every_theme_has_the_dataviz_ramp_for_its_type():
    for tid, t in BUILTIN_THEMES.items():
        ramp = DATAVIZ_RAMP["light" if t["type"] == "light" else "dark"]
        for k, v in ramp.items():
            assert t["vars"].get(k) == v, f"{tid} lacks {k}"
        # nothing the theme had before is gone
        for k in ("--bg", "--s1", "--t1", "--ac", "--on-ac", "--ui-radius"):
            assert k in t["vars"], f"{tid} lost {k}"


def test_with_dataviz_keeps_a_themes_own_ramp():
    v = with_dataviz({"--dv1": "#123456"}, "dark")
    assert v["--dv1"] == "#123456"
    assert v["--dv7"] == DATAVIZ_RAMP["dark"]["--dv7"]


def test_css_emission_has_root_derived_then_every_pack():
    css = appearance_css()
    assert css.startswith(":root {")
    for k in DERIVED_VARS:
        assert k + ":" in css
    for pid in STYLE_PACKS:
        assert f'[data-style="{pid}"]' in css
        assert style_pack_css(pid) in css
    assert style_pack_css("nope") == ""
    # the pack's radius is the one the redesign draws, and it comes after :root
    assert css.index('[data-style="standard"]') > css.index(":root")
    assert "--ui-radius:10px" in style_pack_css("standard")
    assert "--ui-radius:0px" in style_pack_css("terminal")


def test_normalize_appearance_clamps_and_defaults():
    assert normalize_appearance() == {"style": "standard", "density": "full", "blocks": True}
    assert normalize_appearance("PIXEL", "Zen", "off") == {"style": "pixel", "density": "zen", "blocks": False}
    assert normalize_appearance("bogus", "bogus", 0) == {"style": "standard", "density": "full", "blocks": False}
    assert normalize_appearance(None, None, "on")["blocks"] is True
