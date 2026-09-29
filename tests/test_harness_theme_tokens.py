"""
A theme pick sets the theme's OWN tokens (--bg · --s1 · --t1 · --ac · --dv…) as well as the old names they map onto,
so everything drawn on the new tokens follows the pick (the user: a dark pick left the shell light).
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "capability_orchestration.html")


def test_apply_theme_vars_sets_the_themes_own_tokens_then_the_mapped_old_names():
    fn = HTML[HTML.index("function _applyThemeVars(vars, themeId){"):HTML.index("async function _fetchAllThemes(force){")]
    own = fn.index("for(const [k, v] of Object.entries(vars)){ if(/^--/.test(k) && v != null && v !== '') put(k, String(v)); }")
    mapped = fn.index("for(const [src, targets] of Object.entries(_THEME_VAR_MAP)){")
    assert own < mapped, "the theme's own tokens first, then the old names they map onto"
    assert "_writeThemeCache(tid, Object.assign({}, vars, applied));" in fn
