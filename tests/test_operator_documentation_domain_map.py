"""Which doc a panel's screenshots land in is decided by one explicit table."""
import pytest

from vera.operator.docs import domain_map as dm

pytestmark = pytest.mark.critical


def test_every_mapped_slug_is_a_real_domain():
    slugs = set(dm.all_slugs())
    assert {s for s in dm.PANEL_DOCS.values()} <= slugs


def test_no_panel_is_claimed_by_two_docs():
    seen = {}
    for d in dm.DOMAINS:
        for pid in d.get("panel_ids", []):
            assert pid not in seen, f"{pid} in {seen.get(pid)} and {d['slug']}"
            seen[pid] = d["slug"]


@pytest.mark.parametrize("panel,label,expected", [
    # Each of these landed in the wrong doc under substring matching.
    ("interaction-map", "Estate Map", "infrastructure-provisioning"),
    ("workspaces", "Workspaces", "execution"),
    ("markets", "Markets", "markets"),
    ("email-panel", "Email", "integrations"),
    ("dream-panel", "Dream", "dream"),
    ("ml-lab", "ML Lab", "machine-learning"),
    ("perf-monitor", "Perf", "performance"),
])
def test_misplaced_panels_now_land_in_their_own_doc(panel, label, expected):
    assert dm.domain_for_panel({"id": panel, "label": label}) == expected


def test_a_panel_nobody_mapped_is_not_guessed_from_its_name():
    assert dm.domain_for_panel({"id": "ide-ish-dashboard", "label": "IDE memory cap"}) is None
