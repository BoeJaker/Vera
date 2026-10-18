"""P0-6 (owner approved 18 Oct 2026): an adopt gate runs the test files that touch its change."""
from vera.evolve import test_runner_core as tr

SOURCES = {
    "vera/evolve/release_lane_core.py": "import json\n",
    "vera/evolve/release_capabilities.py": "from Vera.vera.evolve import release_lane_core as lane\nfrom x import y\n",
    "vera/evolve/schedule_capabilities.py": "from vera.evolve import release_capabilities\n",
    "vera/evolve/far_away.py": "from vera.evolve import schedule_capabilities\n",
    "vera/other/unrelated.py": "import os\n",
}
TESTS = {
    "test_release_lane.py": "from vera.evolve import release_lane_core as L\n",
    "test_release_wired.py": "SRC = open('vera/evolve/release_capabilities.py').read()\n",
    "test_schedule.py": "import schedule_capabilities\n",
    "test_far.py": "from vera.evolve import far_away\n",
    "test_unrelated.py": "from vera.other import unrelated\n",
    "test_panel.py": "open('vera/evolve/evolve_panel.html')\n",
}
FILES = sorted(TESTS)
IDX = tr.importers_index(SOURCES)


def test_imports_of_reads_from_and_import_forms():
    assert set(tr.imports_of("from Vera.vera.evolve import a_core as A, b\nimport os.path, json as j\n")) >= {"evolve", "a_core", "b", "path", "json"}


def test_a_core_change_selects_its_tests_and_its_importers_tests_within_depth():
    picked, why = tr.impact_select(FILES, ["vera/evolve/release_lane_core.py"], TESTS, IDX, max_share=1.0)
    assert set(picked) == {"test_release_lane.py", "test_release_wired.py", "test_schedule.py", "test_far.py"}
    assert "test_unrelated.py" not in picked and "touch the change" in why


def test_depth_bounds_how_far_importers_reach():
    names = tr.affected_names(["vera/evolve/release_lane_core.py"], IDX, depth=1)
    assert "release_capabilities" in names and "schedule_capabilities" not in names


def test_a_panel_change_selects_tests_naming_the_file():
    picked, _ = tr.impact_select(FILES, ["vera/evolve/evolve_panel.html"], TESTS, IDX, max_share=1.0)
    assert picked == ["test_panel.py"]


def test_a_changed_test_file_is_always_run():
    picked, _ = tr.impact_select(FILES, ["tests/test_unrelated.py"], TESTS, IDX, max_share=1.0)
    assert "test_unrelated.py" in picked


def test_core_files_empty_selections_and_big_selections_run_everything():
    assert tr.impact_select(FILES, ["vera/capability_orchestration.py"], TESTS, IDX)[0] == FILES
    assert tr.impact_select(FILES, ["tests/conftest.py"], TESTS, IDX)[0] == FILES
    allf, why = tr.impact_select(FILES, ["docs/readme.md"], TESTS, IDX)
    assert allf == FILES and "safe default" in why
    big, why2 = tr.impact_select(FILES, ["vera/evolve/release_lane_core.py"], TESTS, IDX, max_share=0.5)
    assert big == FILES and "touch the change" in why2


def test_impact_is_on_by_default_and_can_be_switched_off():
    assert tr.merge_pool_config({})["impact"] is True
    assert tr.merge_pool_config({"impact": False})["impact"] is False


def test_gate_paths_with_the_tests_prefix_work_the_same():
    files = ["tests/" + f for f in FILES]
    texts = {"tests/" + k: v for k, v in TESTS.items()}
    picked, _ = tr.impact_select(files, ["vera/evolve/evolve_panel.html"], texts, IDX, max_share=1.0)
    assert picked == ["tests/test_panel.py"]
    picked, _ = tr.impact_select(files, ["tests/test_unrelated.py"], texts, IDX, max_share=1.0)
    assert "tests/test_unrelated.py" in picked


def test_a_module_reached_through_sys_modules_counts_as_imported():
    assert "evolve_capabilities" in tr.imports_of('em = sys.modules.get("evolve_capabilities") or sys.modules.get("Vera.vera.evolve.evolve_capabilities")')
    idx = tr.importers_index({"vera/evolve/evolve_capabilities.py": "", "vera/evolve/test_runner_capabilities.py": 'sys.modules.get("evolve_capabilities")'})
    assert "test_runner_capabilities" in tr.affected_names(["vera/evolve/evolve_capabilities.py"], idx)


def test_a_hub_that_looks_a_module_up_does_not_spread_its_change_everywhere():
    idx = tr.importers_index({"vera/capability_orchestration.py": 'sys.modules.get("census_capabilities")',
                              "vera/census/census_capabilities.py": "", "vera/x/user.py": "import capability_orchestration"})
    names = tr.affected_names(["vera/census/census_capabilities.py"], idx)
    assert "capability_orchestration" not in names and "user" not in names
