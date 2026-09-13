"""Which top-level tabs open inside a broader tab, where each one opens, and the
one setting that brings them back. Also pins that map to the real panes of each
host panel, and the Models tab to the Estate panel's models view."""
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
# The other panels that took tabs in, keyed by panel id.
HOSTS = {
    "cap-hub": os.path.join(ROOT, "vera", "capabilities", "cap_hub.html"),
    "agents-skills-ontologies": os.path.join(ROOT, "vera", "agents_skills_ontologies_panel.html"),
    "image-studio": os.path.join(ROOT, "vera", "images", "image_studio_panel.html"),
}
ESTATE = {pid: t for pid, t in nav.RETIRED_TABS.items() if t["panel"] == "workers-ollama"}
OTHERS = {pid: t for pid, t in nav.RETIRED_TABS.items() if t["panel"] != "workers-ollama"}


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_the_overlapping_tabs_open_inside_estate():
    assert set(ESTATE) == {
        "proxmox-panel", "provision-panel", "provisioning-panel", "remote-connections",
        "identity-panel", "integrations", "netgraph-panel", "platform-config"}


def test_capabilities_agents_and_image_studio_take_in_their_tabs():
    assert {pid: (t["panel"], t["pane"]) for pid, t in OTHERS.items()} == {
        "cap-ontology": ("cap-hub", "ontology"),
        "mcp-catalog-panel": ("cap-hub", "mcp"),
        "agentbridge-catalog-panel": ("agents-skills-ontologies", "bridges"),
        "character-studio": ("image-studio", "companion"),
    }


def test_every_target_pane_exists_in_the_estate_panel_and_its_list():
    html = _read(PANEL)
    subs = {"provision": re.search(r"const _prvSubSrc=\{([^}]*)\}", html).group(1),
            "network": re.search(r"const _netSubSrc=\{([^}]*)\}", html).group(1)}
    for pid, t in ESTATE.items():
        assert f'id="pane-{t["pane"]}"' in html, pid
        assert f'data-pane="{t["pane"]}" data-view="estate"' in html, pid
        if t["sub"]:
            assert f'{t["sub"]}:' in subs[t["pane"]], pid


def test_every_other_host_has_the_pane_and_answers_the_open_request():
    for pid, t in OTHERS.items():
        html = _read(HOSTS[t["panel"]])
        assert f'id="pane-{t["pane"]}"' in html, pid
        assert re.search(r'data-(?:pane|go)="%s"' % re.escape(t["pane"]), html), pid
        # The shell repeats vera:estate:open until the host acknowledges it.
        assert "'vera:estate:open'" in html and "'vera:estate:opened'" in html, pid
    assert 'id="ms-catalog"' in _read(HOSTS["cap-hub"])


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
    tree = ast.parse(_read(WORKERS))
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
    html = _read(PANEL)
    for pane in ("ollama", "modelrouting", "mimic", "vllm", "api"):
        assert f'data-pane="{pane}" data-view="models"' in html, pane
        assert f'data-pane="{pane}" data-view="estate"' not in html, pane
