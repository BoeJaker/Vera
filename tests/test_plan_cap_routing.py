"""A step that changes a file an earlier step created must not be an author step.

The fixture below is the REAL plan from session `chat-1788039334205` (goal:
"create a pokedex in html js and css that looks like a pokedex", 2026-08-30).
The planner split one self-contained file into four sequential `code.author`
steps against `index.html`, while its own success text for steps 2-4 said the
file "is updated by code.edit" — it named the right cap in one field and emitted
the wrong one in the other.

Each `code.author` call on an existing file is a full re-emit, and a re-emit is
what drops the previous step's work — the cost `_V5_CORE_SEED_CAPS` already
documents in its own comment.

Pure: no Redis, no app import - so it runs anywhere, including the host venv.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.dag.plan_cap_routing import (          # noqa: E402
    CREATE_CAP, EDIT_CAP, classify, files_named, route_edit_steps,
)

# ── the real plan, verbatim from the run ────────────────────────────────────
POKEDEX = [
    {"id": 1, "title": "Create self-contained Pokedex HTML file",
     "caps": ["code.author"],
     "success": "index.html is created and verified by code.author, containing the "
                "full DOM structure for a handheld console chassis"},
    {"id": 2, "title": "Populate Pokedex with initial Pokemon data",
     "caps": ["code.author"],
     "success": "index.html is updated by code.edit to include a JS array of at "
                "least 3-5 Pokemon entries and a rendering function."},
    {"id": 3, "title": "Implement Search/Filter functionality",
     "caps": ["code.author"],
     "success": "index.html is updated by code.edit to include a text input element "
                "and event listener that filters the rendered list."},
    {"id": 4, "title": "Add Audio Feedback for Interactions",
     "caps": ["code.author"],
     "success": "index.html is updated by code.edit to include an HTML5 Audio "
                "element and JS logic that plays sounds."},
    {"id": 5, "title": "Verify Visual Layout and Responsiveness",
     "caps": ["operator.run"],
     "success": "A headless browser successfully loads index.html."},
]


def _caps_by_id(steps):
    return {s["id"]: s.get("caps") for s in steps}


def test_the_pokedex_plan_is_repaired_end_to_end():
    routed, changes = route_edit_steps(POKEDEX)
    assert _caps_by_id(routed) == {
        1: ["code.author"],        # genuinely creates the file - untouched
        2: ["code.edit"],
        3: ["code.edit"],
        4: ["code.edit"],
        5: ["operator.run"],       # not an authoring step at all - untouched
    }
    assert [c["id"] for c in changes] == [2, 3, 4]


def test_the_creating_step_is_never_rerouted():
    """Step 1 is the only one that really creates index.html. Re-routing it to
    code.edit would leave the run with nothing to edit."""
    routed, _ = route_edit_steps(POKEDEX)
    assert routed[0]["caps"] == [CREATE_CAP]


def test_the_change_is_recorded_not_silent():
    """A silent repair just moves the blind spot somewhere else."""
    _, changes = route_edit_steps(POKEDEX)
    c = changes[0]
    assert c["from"] == ["code.author"] and c["to"] == ["code.edit"]
    assert c["files"] == ["index.html"]
    assert "code.edit" in c["reason"]


def test_the_original_plan_is_not_mutated():
    before = _caps_by_id(POKEDEX)
    route_edit_steps(POKEDEX)
    assert _caps_by_id(POKEDEX) == before


# ── the two independent signals ────────────────────────────────────────────
def test_a_step_that_names_code_edit_is_an_edit_even_with_no_known_file():
    """The self-contradiction alone is enough: the planner already knows."""
    steps = [{"id": 1, "caps": ["code.author"],
              "success": "app.py is updated by code.edit to add a retry"}]
    routed, changes = route_edit_steps(steps)
    assert routed[0]["caps"] == ["code.edit"]
    assert len(changes) == 1


def test_a_step_touching_only_already_created_files_is_an_edit():
    """No mention of code.edit anywhere — inferred purely from plan order."""
    steps = [
        {"id": 1, "title": "Create app.py", "caps": ["code.author"]},
        {"id": 2, "title": "Add logging to app.py", "caps": ["code.author"]},
    ]
    routed, _ = route_edit_steps(steps)
    assert routed[1]["caps"] == ["code.edit"]


# ── what must NOT be touched ───────────────────────────────────────────────
def test_a_step_creating_a_new_file_keeps_code_author():
    steps = [
        {"id": 1, "title": "Create app.py", "caps": ["code.author"]},
        {"id": 2, "title": "Create helpers.py", "caps": ["code.author"]},
    ]
    routed, changes = route_edit_steps(steps)
    assert routed[1]["caps"] == ["code.author"] and changes == []


def test_a_step_naming_a_mix_of_new_and_existing_files_keeps_code_author():
    """It still has something to create, so it is not purely an edit."""
    steps = [
        {"id": 1, "title": "Create app.py", "caps": ["code.author"]},
        {"id": 2, "title": "Split app.py into cli.py", "caps": ["code.author"]},
    ]
    routed, changes = route_edit_steps(steps)
    assert routed[1]["caps"] == ["code.author"] and changes == []


def test_a_step_naming_no_file_is_left_alone():
    steps = [
        {"id": 1, "title": "Create app.py", "caps": ["code.author"]},
        {"id": 2, "title": "Improve the error handling", "caps": ["code.author"]},
    ]
    routed, changes = route_edit_steps(steps)
    assert routed[1]["caps"] == ["code.author"] and changes == []


def test_a_step_that_already_has_code_edit_is_left_alone():
    steps = [
        {"id": 1, "title": "Create app.py", "caps": ["code.author"]},
        {"id": 2, "title": "Update app.py", "caps": ["code.author", "code.edit"]},
    ]
    routed, changes = route_edit_steps(steps)
    assert routed[1]["caps"] == ["code.author", "code.edit"] and changes == []


def test_non_authoring_caps_are_untouched():
    steps = [
        {"id": 1, "title": "Create app.py", "caps": ["code.author"]},
        {"id": 2, "title": "Run app.py", "caps": ["exec.python.run"]},
    ]
    routed, changes = route_edit_steps(steps)
    assert routed[1]["caps"] == ["exec.python.run"] and changes == []


def test_other_caps_on_a_rerouted_step_survive_in_order():
    steps = [
        {"id": 1, "title": "Create app.py", "caps": ["code.author"]},
        {"id": 2, "title": "Update app.py", "caps": ["ide.fs.read", "code.author",
                                                     "exec.python.run"]},
    ]
    routed, _ = route_edit_steps(steps)
    assert routed[1]["caps"] == ["ide.fs.read", "code.edit", "exec.python.run"]


def test_a_prose_file_is_not_this_modules_business():
    """Documents route to prose.author; .md must not register as a code file."""
    assert files_named({"title": "Write README.md"}) == set()
    steps = [
        {"id": 1, "title": "Create README.md", "caps": ["code.author"]},
        {"id": 2, "title": "Update README.md", "caps": ["code.author"]},
    ]
    assert route_edit_steps(steps)[1] == []


# ── path spellings of the same file ────────────────────────────────────────
def test_the_same_file_spelled_differently_is_the_same_file():
    """The planner is inconsistent about paths; treating these as different
    files is how a real edit step gets missed."""
    steps = [
        {"id": 1, "title": "Create /workspace/index.html", "caps": ["code.author"]},
        {"id": 2, "title": "Update ./index.html with a footer", "caps": ["code.author"]},
    ]
    routed, _ = route_edit_steps(steps)
    assert routed[1]["caps"] == ["code.edit"]


def test_file_extraction_finds_code_files_and_ignores_prose():
    got = files_named({"title": "edit src/app.py and styles.css",
                       "success": "also notes.txt"})
    assert got == {"app.py", "styles.css"}


# ── robustness: a repair must never be the thing that breaks a run ─────────
def test_malformed_steps_do_not_explode():
    steps = [None, {"id": 1, "caps": "not-a-list"}, {"id": 2}, {}]
    routed, changes = route_edit_steps(steps)
    assert len(routed) == 4 and changes == []


def test_an_empty_plan_is_fine():
    assert route_edit_steps([]) == ([], [])
    assert classify([]) == []


# ── the wiring itself ──────────────────────────────────────────────────────
# A repair that is correct but never called is inert, and inertness is silent —
# the same failure mode that made _V7_CRITERIA_RULE do nothing on every real run
# for weeks. Source-level so it needs no app import.
_MODULE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "vera", "dag", "dag_workshop_capabilities.py")


def _module_src():
    with open(_MODULE, encoding="utf-8") as fh:
        return fh.read()


def test_both_runners_route_edit_steps_before_emitting_the_plan():
    """v5/v6 and v7 each have their own plan choke point. A rule that reaches
    only one runner is exactly the drift test_planner_rule_parity exists for —
    and the plan the USER sees must be the plan that runs, so the call has to
    come before the emit, not after."""
    src = _module_src()
    # "await " so the `async def` line itself is not counted as a call site.
    calls = src.count("await _v5_route_edit_steps(steps")
    assert calls == 2, f"expected both runners wired, found {calls} call site(s)"

    for emit in ('"type": "agent_loop_v5.plan"', '"type": "agent_loop_v6.plan"'):
        at = src.find(emit)
        assert at > 0, f"{emit} not found"
        window = src[max(0, at - 600):at]
        assert "_v5_route_edit_steps(steps" in window, (
            f"{emit} is emitted without routing edit steps first — the displayed "
            f"plan would not match the plan that runs")


def test_the_reroute_event_can_reach_the_stream():
    """An event type missing from the allowlist is dropped silently."""
    assert '"agent_loop_v6.cap_reroute"' in _module_src()
