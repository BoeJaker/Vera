"""The dashboard widgets on the Harness board's surfaces (B3): the head's title as the label, the counters as hero numbers."""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace") as fh:
        return fh.read()


HTML = _read("vera", "capability_orchestration.html")


def test_the_widget_head_is_the_label_and_the_counters_are_hero_numbers():
    css = HTML[HTML.index('<style id="designHarness">'):HTML.index("</style>", HTML.index('<style id="designHarness">'))]
    assert ".w-title{font-family:var(--f-ui,var(--sans,system-ui,sans-serif));font-size:var(--label-size,9px);font-weight:600;letter-spacing:.09em;text-transform:uppercase" in css
    assert ".w-body .kpi-val{font-family:var(--f-mono,var(--mono,ui-monospace,monospace));font-size:34px!important" in css
    assert ".w-body .kpi-sub{" in css and ".w-head{padding:11px 12px 0" in css
    assert 'class="kpi-val' in HTML and 'class="kpi-sub"' in HTML, "the counters' own markup stays"
