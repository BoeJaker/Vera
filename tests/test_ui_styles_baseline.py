"""Style unification, phase A: one baseline under every panel (zero-specificity,
so a panel's own rule always wins), the theme reaching every colour var, one
button vocabulary, the UI font on every body, and no series colour named like
a surface."""
import glob
import os
import re
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

pytestmark = pytest.mark.critical

GROUPS = [[".btn.primary", ".btn.pri"],
          [".btn.danger", ".btn.red", ".btn.err-btn", ".btn.no"],
          [".btn.ok", ".btn.grn", ".btn.ok-btn"]]


def read(p):
    return open(p, encoding="utf-8").read()


def panels():
    return sorted(set(glob.glob(os.path.join(ROOT, "vera", "*_panel.html")) + glob.glob(os.path.join(ROOT, "vera", "*", "*_panel.html"))))


def test_the_theme_reaches_every_colour_var_and_the_baseline_is_zero_specificity():
    js = read(os.path.join(ROOT, "vera", "vera-ui.js"))
    for var in ("--red", "--green", "--yellow", "--line", "--hi", "--panel", "--muted"):
        assert f"set('{var}'," in js, var
    b = js[js.index("function baseline()"):js.index("// ── 2b.")]
    assert "vera-ui-baseline" in b and "--font-ui" in b and "--font-mono" in b
    rules = re.findall(r"'([^']*\{[^']*\})'", b)
    assert len(rules) >= 12
    for r in rules:
        assert r.startswith(":root{") or r.startswith(":where("), r[:60] + " - only :root and :where() rules; a panel's own rule must always win"
    assert ":where(.btn.pri,.btn.primary)" in b and ":where(.btn.danger,.btn.red,.btn.err-btn,.btn.no)" in b and ":where(.btn.ok,.btn.grn,.btn.ok-btn)" in b


def test_every_panel_boots_the_theme_and_loads_vera_ui():
    missing_boot, missing_ui = [], []
    for p in panels():
        s = read(p)
        if "<head>" in s and "vera theme boot" not in s:
            missing_boot.append(os.path.basename(p))
        if "</body>" in s and "/ui/vera-ui.js" not in s:
            missing_ui.append(os.path.basename(p))
    assert not missing_boot, missing_boot
    assert not missing_ui, missing_ui


def tok(t):
    return re.compile(re.escape(t) + r"(?![\w-])")


def test_every_button_rule_names_all_spellings_of_its_kind():
    gaps = []
    for p in panels():
        for css in re.findall(r"<style[^>]*>(.*?)</style>", read(p), flags=re.S):
            for m in re.finditer(r"([^{};]+)\{", css):
                sel = m.group(1)
                if sel.lstrip().startswith("@"):
                    continue
                parts = [x.strip() for x in sel.split(",") if x.strip()]
                for grp in GROUPS:
                    for x in parts:
                        hits = [t for t in grp if tok(t).search(x)]
                        if len(hits) != 1:
                            continue
                        for other in grp:
                            if other != hits[0] and tok(hits[0]).sub(other, x) not in parts:
                                gaps.append((os.path.basename(p), x))
    assert not gaps[:10], gaps[:10]


def test_no_body_reads_as_a_terminal_and_no_series_colour_is_a_surface():
    mono = re.compile(r"(?:html,\s*body|body,\s*html|body)\s*\{[^}]*?font:\s*[\d.]+px(?:/[\d.]+)?\s+(?:'ui-monospace'|ui-monospace|var\(--mono\)|monospace)")
    bad = [os.path.basename(p) for p in panels() if mono.search(read(p))]
    assert not bad, bad
    for rel in ("business/business_panel.html", "commerce/commerce_panel.html", "commerce/commerce_uk_panel.html", "markets/markets_panel.html"):
        s = read(os.path.join(ROOT, "vera", *rel.split("/")))
        assert not re.search(r"--s[123]:", s), rel
        assert "--series1" in s, rel
