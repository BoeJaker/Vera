"""Which top-level tabs Estate replaces, where each one now opens, and the one
setting that brings them back. Also pins that map to the Estate panel's real
panes and the Models tab to the same panel's models view."""
import ast
import os
import re
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from vera.estate import estate_nav_core as nav  # noqa: E402

pytestmark = pytest.mark.critical

PANEL = os.path.join(ROOT, "vera", "workers", "workers_ollama_panel.html")
WORKERS = os.path.join(ROOT, "vera", "workers", "workers.py")


def test_the_seven_overlapping_tabs_open_inside_estate():
    assert set(nav.RETIRED_TABS) == {
        "proxmox-panel", "provision-panel", "provisioning-panel", "remote-connections",
        "identity-panel", "integrations", "netgraph-panel"}
    assert all(t["panel"] == "workers-ollama" for t in nav.RETIRED_TABS.values())


def test_every_target_pane_exists_in_the_estate_panel_and_its_list():
    html = open(PANEL, encoding="utf-8").read()
    subs = {"provision": re.search(r"const _prvSubSrc=\{([^}]*)\}", html).group(1),
            "network": re.search(r"const _netSubSrc=\{([^}]*)\}", html).group(1)}
    for pid, t in nav.RETIRED_TABS.items():
        assert f'id="pane-{t["pane"]}"' in html, pid
        assert f'data-pane="{t["pane"]}" data-view="estate"' in html, pid
        if t["sub"]:
            assert f'{t["sub"]}:' in subs[t["pane"]], pid


@pytest.mark.parametrize("raw,expected", [
    (None, True), ("1", True), (b"1", True), (True, True), ("", True),
    ("0", False), (b"0", False), ("off", False), ("false", False), (False, False),
])
def test_the_setting_defaults_to_retiring(raw, expected):
    assert nav.setting_enabled(raw) is expected


def test_annotation_marks_only_retired_tabs_and_leaves_the_registry_alone():
    panels = [{"id": "proxmox-panel", "mode": "tab"}, {"id": "workers-ollama", "mode": "tab"},
              {"id": "integrations", "mode": "element"}]
    out = nav.annotate_panels(panels, True)
    assert out[0]["retired_into"] == nav.RETIRED_TABS["proxmox-panel"]
    assert "retired_into" not in out[1] and "retired_into" not in out[2]
    assert "retired_into" not in panels[0]
    out[0]["retired_into"]["pane"] = "changed"
    assert nav.RETIRED_TABS["proxmox-panel"]["pane"] == "proxmox"
    assert all("retired_into" not in p for p in nav.annotate_panels(panels, False))


def test_the_models_tab_opens_the_same_panel_in_its_models_view():
    tree = ast.parse(open(WORKERS, encoding="utf-8").read())
    wanted = {"_WOL_MOUNT_JS", "_MODELS_MOUNT_JS"}
    assigns = [n for n in tree.body if isinstance(n, ast.Assign)
               and any(getattr(t, "id", "") in wanted for t in n.targets)]
    scope = {}
    exec(compile(ast.Module(body=assigns, type_ignores=[]), WORKERS, "exec"), scope)
    models = scope["_MODELS_MOUNT_JS"]
    assert "'/ui/panels/workers-ollama?view=models'" in models
    assert "getElementById('panel-models')" in models
    assert "auto-models" in models and "auto-workers-ollama" not in models
    assert "'/ui/panels/workers-ollama'" in scope["_WOL_MOUNT_JS"]


def test_every_models_pane_is_listed_only_in_the_models_view():
    html = open(PANEL, encoding="utf-8").read()
    for pane in ("ollama", "modelrouting", "mimic", "vllm", "api"):
        assert f'data-pane="{pane}" data-view="models"' in html, pane
        assert f'data-pane="{pane}" data-view="estate"' not in html, pane
