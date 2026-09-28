"""A plan holds only what the goal asked for (plan item 22).

run70-73 (24 Sep 2026): 17 of 40 plans carried a "Verify ..." step re-checking
settled work, 9 success criteria added features the goal never named, and a
one-command goal ("Report the disk usage ...") was planned as run + parse +
"report" twice - and the completion gate then appended "Write and save the
report" because it read the verb as a document.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vera.dag import plan_hygiene_core as H  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
TIMER_GOAL = ("Create timer.html with a 60 second countdown, then change the countdown to "
              "90 seconds leaving everything else identical.")
TIMER_PLAN = [
    {"id": 1, "title": "Create timer.html with 60-second countdown", "caps": ["code.author"], "needs": [],
     "success": "timer.html is created and verified by code.author, displaying '60' initially, "
                "pausing correctly, resetting to 60, and persisting state in localStorage."},
    {"id": 2, "title": "Change the countdown to 90 seconds", "caps": ["code.edit"], "needs": [1],
     "success": "timer.html now counts from 90."},
    {"id": 3, "title": "Verify 90-second countdown behavior", "caps": ["operator.run"], "needs": [2],
     "success": "operator.run observes the initial display as '89' and validates pause/reset."},
]
FORM_GOAL = ("Create form.html with an email input that shows an inline validation error for a "
             "malformed address, and verify in a browser that the error actually appears.")
DISK_GOAL = "Report the disk usage of /workspace and list the five largest files under it."
DISK_PLAN = [
    {"id": 1, "title": "Run disk usage command on /workspace", "caps": ["exec.bash.run"], "needs": []},
    {"id": 2, "title": "List files by size in descending order", "caps": ["exec.bash.run"], "needs": [1]},
    {"id": 3, "title": "Parse and format the results into a report",
     "caps": ["prose.author", "exec.python.run"], "needs": [2]},
]


def test_a_browser_check_the_goal_never_asked_for_is_dropped():
    steps, notes = H.drop_verify_steps(TIMER_GOAL, TIMER_PLAN)
    assert [s["title"] for s in steps] == [TIMER_PLAN[0]["title"], TIMER_PLAN[1]["title"]]
    assert steps[1]["needs"] == [1] and steps[1]["id"] == 2
    assert len(notes) == 1 and "browser check the goal never asked for" in notes[0]


def test_the_check_the_goal_asked_for_stays():
    plan = [{"id": 1, "title": "Create form.html", "caps": ["code.author"], "needs": []},
            {"id": 2, "title": "Verify the inline error appears in a browser", "caps": ["operator.run"],
             "needs": [1]}]
    steps, notes = H.drop_verify_steps(FORM_GOAL, plan)
    assert len(steps) == 2 and notes == []


def test_a_read_back_of_an_authored_file_is_dropped():
    goal = "Research the current state of WebGPU support and write a short summary citing your sources."
    plan = [{"id": 1, "title": "Research WebGPU support", "caps": ["web.research"], "needs": []},
            {"id": 2, "title": "Write the summary", "caps": ["prose.author"], "needs": [1]},
            {"id": 3, "title": "Verify document content and formatting",
             "caps": ["prose.author", "sandbox.session.fs.read"], "needs": [2]}]
    steps, notes = H.drop_verify_steps(goal, plan)
    assert [s["id"] for s in steps] == [1, 2] and "read-back" in notes[0]


def test_a_plan_that_is_only_a_check_is_left_alone():
    plan = [{"id": 1, "title": "Check the service responds", "caps": ["http.get"], "needs": []}]
    assert H.drop_verify_steps("Is the API up?", plan) == (plan, [])


def test_added_requirements_name_the_features_the_goal_never_had():
    added = H.added_requirements(TIMER_GOAL, TIMER_PLAN)
    terms = {a["term"] for a in added}
    assert {"pause", "reset", "localStorage"} <= terms
    assert not H.added_requirements("Create a timer with pause and reset buttons that persists in localStorage",
                                    TIMER_PLAN[:1])
    note = H.hygiene_note(added)
    assert "pause" in note and "ONLY what the GOAL states" in note


def test_the_verb_report_is_not_a_document():
    assert not H.implies_document(DISK_GOAL)
    assert not H.implies_document("Generate 200 random integers, save them to nums.csv, then report their mean.")
    assert H.implies_document("Produce a short report on the Redis licensing change, with citations.")
    assert H.implies_document("Research WebGPU support and write a short summary citing your sources.")


def test_a_one_command_goal_becomes_one_exec_step():
    assert H.is_single_command_goal(DISK_GOAL, DISK_PLAN, has_seam=lambda g: " then " in g)
    steps, note = H.merge_exec_plan(DISK_GOAL, DISK_PLAN)
    assert len(steps) == 1 and steps[0]["caps"] == ["exec.bash.run", "exec.python.run"]
    assert steps[0]["goal"] == DISK_GOAL and "answers the goal" in steps[0]["success"]
    assert "merged 3 steps" in note


def test_a_goal_with_a_seam_or_a_file_is_not_merged():
    seam = "Generate 200 random integers, save them to /workspace/nums.csv, then report their mean."
    assert not H.is_single_command_goal(seam, DISK_PLAN, has_seam=lambda g: " then " in g)
    assert not H.is_single_command_goal("Report the disk usage and save it to usage.txt", DISK_PLAN[:2])
    assert not H.is_single_command_goal(DISK_GOAL, [DISK_PLAN[0]])


def _src():
    return open(os.path.join(ROOT, "vera", "dag", "dag_workshop_capabilities.py"), encoding="utf-8").read()


def test_the_runner_applies_hygiene_before_the_plan_is_emitted():
    src = _src()
    assert src.index("_plan_hygiene.added_requirements(_orig_goal, steps)") < src.index('"type": "agent_loop_v6.plan",')
    assert "_plan_hygiene.drop_verify_steps(_orig_goal, steps)" in src
    assert "_plan_hygiene.merge_exec_plan(_orig_goal, steps)" in src
    assert "plan_note=_plan_hygiene.hygiene_note(_added)" in src


def test_the_gate_reads_the_document_noun_not_the_verb():
    src = _src()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_v6_final_gate")
    body = ast.get_source_segment(src, fn)
    assert "_v6_goal_implies_document(" in body
    assert "_V5_PROSE_STEP_NOUN_RE.search" not in body


def test_the_action_directive_asks_for_no_separate_verify_step():
    src = _src()
    assert "do NOT add a separate verify step" in src
    assert "perform and then VERIFY the operation" not in src


def test_the_criteria_rule_no_longer_seeds_pause_and_reset():
    from vera.dag import loop_prompt_rules as R
    text = R.RULES["criteria_settleable"].text
    assert "start/pause/reset controls" not in text
    assert "A criterion may test ONLY what the GOAL states" in text
    assert "plan one ONLY when the GOAL asks" in text
