"""A custom theme's vars are one flat {--name: value} map, whatever shape they were saved in (2026-10-04: the active
custom theme had them one level down, {"theme": {...}}, and no page set a colour from it)."""
from Vera.vera.theme_defs import DERIVED_VARS, flat_theme_vars


def test_a_theme_saved_one_level_down_is_read_from_inside():
    nested = {"theme": {"--bg": "#f5e9dc", "--ac": "#e6908a", "--s1": "#ffdab9"}}
    assert flat_theme_vars(nested) == {"--bg": "#f5e9dc", "--ac": "#e6908a", "--s1": "#ffdab9"}


def test_a_flat_theme_is_kept_and_non_values_dropped():
    assert flat_theme_vars({"--bg": "#000", "--ui-radius": "5px", "label": "x", "--n": 3, "--bad": {"a": 1}}) == \
        {"--bg": "#000", "--ui-radius": "5px", "--n": "3"}


def test_nothing_usable_is_an_empty_map():
    assert flat_theme_vars(None) == {}
    assert flat_theme_vars({"a": {"b": 1}, "c": {"d": 2}}) == {}


def test_the_derived_surfaces_fall_back_to_the_older_names():
    for k, v in DERIVED_VARS.items():
        assert "var(--ac)" not in v and "var(--s1)" not in v, k
        assert "var(--acc," in v, k
