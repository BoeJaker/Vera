"""A contended census run must not be read as a regression.

Run 41 cost 3.8 hours and scored 7/12 against 11/12 and 10/12 either side. As a
score that is a large regression. It was not one — the network was contended,
confirmed by the operator, and the harness could not tell because it only checks
the GPU gate and the running-loop count, both of which read free throughout.

Every fixture below is the REAL data from runs 40, 41 and 42.

The discriminator is a contrast, not a threshold: a slow box slows everything,
whereas run 41 slowed exactly one class of goal while the others got FASTER.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.census import run_health as RH                    # noqa: E402


# The default template's declared axes — intent/output are what mark a goal as
# network-bound; the ids are incidental.
TEMPLATE = {"name": "default", "goals": [
    {"id": "build-simple-code", "intent": "build", "output": "code"},
    {"id": "build-multifile", "intent": "build", "output": "code"},
    {"id": "build-browser-verified", "intent": "build", "output": "browser"},
    {"id": "author-then-edit", "intent": "fix", "output": "code"},
    {"id": "research-web", "intent": "research", "output": "prose"},
    {"id": "research-report", "intent": "research", "output": "report"},
    {"id": "analyse-data", "intent": "analyse", "output": "data"},
    {"id": "operate-exec", "intent": "operate", "output": "data"},
    {"id": "prose-only", "intent": "build", "output": "prose"},
    {"id": "underspecified", "intent": "fix", "output": "code"},
    {"id": "long-horizon", "intent": "build", "output": "code"},
    {"id": "trivial-chat", "intent": "chat", "output": "none"},
]}


def _run(pairs):
    return {gid: {"status": st, "wall_s": w} for gid, st, w in pairs}


RUN40 = _run([
    ("build-simple-code", "done", 247), ("build-multifile", "done", 982),
    ("build-browser-verified", "done", 1035), ("author-then-edit", "wall-cap", 1813),
    ("research-web", "done", 1422), ("research-report", "done", 1765),
    ("analyse-data", "done", 759), ("operate-exec", "done", 382),
    ("prose-only", "done", 867), ("underspecified", "done", 684),
    ("long-horizon", "done", 412), ("trivial-chat", "done", 96)])

RUN41 = _run([
    ("build-simple-code", "done", 188), ("build-multifile", "done", 1107),
    ("build-browser-verified", "wall-cap", 1813), ("author-then-edit", "wall-cap", 1810),
    ("research-web", "wall-cap", 1813), ("research-report", "wall-cap", 1806),
    ("analyse-data", "done", 1652), ("operate-exec", "done", 402),
    ("prose-only", "done", 549), ("underspecified", "wall-cap", 1812),
    ("long-horizon", "done", 517), ("trivial-chat", "done", 95)])

RUN42 = _run([
    ("build-simple-code", "done", 367), ("build-multifile", "done", 442),
    ("build-browser-verified", "done", 895), ("author-then-edit", "wall-cap", 1813),
    ("research-web", "done", 879), ("research-report", "wall-cap", 1801),
    ("analyse-data", "done", 673), ("operate-exec", "done", 442),
    ("prose-only", "done", 988), ("underspecified", "done", 1014),
    ("long-horizon", "done", 611), ("trivial-chat", "done", 111)])


# ── which goals depend on the network ───────────────────────────────────────
def test_research_and_browser_goals_are_network_bound():
    s = RH.split_goals(TEMPLATE)
    assert set(s["network"]) == {"research-web", "research-report",
                                 "build-browser-verified"}


def test_the_rest_are_compute_bound():
    s = RH.split_goals(TEMPLATE)
    assert "analyse-data" in s["compute"] and "build-multifile" in s["compute"]
    assert "research-web" not in s["compute"]


def test_it_reads_the_templates_axes_not_a_goal_list():
    """So it holds for a template nobody has written yet."""
    assert RH.network_bound({"intent": "research"}) is True
    assert RH.network_bound({"output": "browser"}) is True
    assert RH.network_bound({"intent": "build", "output": "code"}) is False
    assert RH.network_bound(None) is False


# ── the real verdicts ───────────────────────────────────────────────────────
def test_run_41_is_called_environmental():
    v = RH.assess(RUN41, RUN40, TEMPLATE)
    assert v["environmental"] is True
    assert set(v["network_degraded"]) == {"research-web", "research-report",
                                          "build-browser-verified"}
    assert "exclude it from any trend" in v["note"]


def test_run_41s_compute_goals_held_their_times():
    """The fact that settles it: a slow box slows everything, and run 41's
    compute goals did not move."""
    ids = RH.split_goals(TEMPLATE)["compute"]
    drift = RH.compute_drift(RUN41, RUN40, ids)
    assert abs(drift) <= RH.COMPUTE_DRIFT


def test_one_outlier_cannot_speak_for_the_compute_set():
    """analyse-data went 759s -> 1652s in run 41. On a ratio-of-totals that
    single goal drags the set to +20.4% and buries the six that were faster;
    on a median it reads +5.2%. Dropping the outlier instead would be how an
    instrument starts lying, so it is resisted, not excluded."""
    ids = RH.split_goals(TEMPLATE)["compute"]
    drift = RH.compute_drift(RUN41, RUN40, ids)
    assert drift < 0.10, "one outlier is dominating the compute drift"
    # and it is genuinely still in the data being measured
    assert RUN41["analyse-data"]["wall_s"] / RUN40["analyse-data"]["wall_s"] > 2


def test_run_42_is_NOT_called_environmental():
    """It degraded research-report but IMPROVED research-web and
    build-browser-verified. One goal moving is variance against the cap."""
    v = RH.assess(RUN42, RUN40, TEMPLATE)
    assert v["environmental"] is False
    assert v["network_degraded"] == ["research-report"]
    assert "ordinary variance" in v["note"]


def test_a_run_compared_with_itself_is_clean():
    v = RH.assess(RUN40, RUN40, TEMPLATE)
    assert v["environmental"] is False
    assert v["network_degraded"] == []


# ── what must NOT be called environmental ───────────────────────────────────
def test_a_genuinely_slower_box_is_not_a_network_signature():
    """Everything slower, including compute — that is a slow box or a real
    regression, and it must not be excused as contention."""
    slow = {g: {"status": r["status"], "wall_s": r["wall_s"] * 1.6}
            for g, r in RUN40.items()}
    for g in ("research-web", "research-report", "build-browser-verified"):
        slow[g] = {"status": "wall-cap", "wall_s": 1813}
    v = RH.assess(slow, RUN40, TEMPLATE)
    assert v["environmental"] is False
    assert "not network-specific" in v["note"]


def test_a_compute_failure_does_not_by_itself_rule_it_out():
    """Run 41 — the CONFIRMED contended run — also lost `underspecified`, a
    compute goal. Demanding a clean split would make this unable to detect the
    one run it exists for. 3 of 3 network against 1 of 7 compute is still a
    class being hit."""
    v = RH.assess(RUN41, RUN40, TEMPLATE)
    assert v["compute_degraded"] == ["underspecified"]
    assert v["environmental"] is True


def test_similar_failure_rates_in_both_classes_rule_it_out():
    """If compute fails at a comparable rate, it is a slow box or a real
    regression and must not be excused as contention."""
    mixed = dict(RUN41)
    for g in ("analyse-data", "build-multifile", "prose-only", "long-horizon"):
        mixed[g] = {"status": "wall-cap", "wall_s": 1813}
    v = RH.assess(mixed, RUN40, TEMPLATE)
    assert v["environmental"] is False
    assert "similar rates" in v["note"] or "not network-specific" in v["note"]


def test_one_network_goal_degrading_is_not_enough():
    one = dict(RUN40)
    one["research-web"] = {"status": "wall-cap", "wall_s": 1813}
    assert RH.assess(one, RUN40, TEMPLATE)["environmental"] is False


def test_a_goal_that_never_passed_in_the_baseline_cannot_degrade():
    """author-then-edit wall-caps in every run; it is not evidence of anything
    changing."""
    v = RH.assess(RUN41, RUN40, TEMPLATE)
    assert "author-then-edit" not in v["network_degraded"]
    assert "author-then-edit" not in v["compute_degraded"]


# ── arithmetic honesty ──────────────────────────────────────────────────────
def test_a_wall_capped_goal_is_excluded_from_the_drift():
    """It contributes the CAP, not its work.

    Written after TWO mutation survivals, and the second is the instructive
    one. The real fixtures cap a goal whose baseline was also the cap
    (author-then-edit, 1813 vs 1813, ratio 1.0), so including it moved nothing.
    Rebuilt with two capped goals — still survived, because a median is
    designed to shrug off two outliers in five.

    So the guard only changes the answer when capping is WIDESPREAD, which is
    exactly the degraded run where the drift number matters most. Three of five
    capped from a low baseline: excluded, the two survivors say the box held;
    counted, the median becomes a 12x "slowdown" that is really just the cap.
    """
    tmpl = {"goals": [{"id": "a", "intent": "build", "output": "code"},
                      {"id": "b", "intent": "build", "output": "code"},
                      {"id": "c", "intent": "build", "output": "code"},
                      {"id": "d", "intent": "build", "output": "code"},
                      {"id": "e", "intent": "build", "output": "code"}]}
    base = _run([("a", "done", 100), ("b", "done", 100), ("c", "done", 150),
                 ("d", "done", 150), ("e", "done", 150)])
    run = _run([("a", "done", 105), ("b", "done", 95),
                ("c", "wall-cap", 1800), ("d", "wall-cap", 1800),
                ("e", "wall-cap", 1800)])
    ids = RH.split_goals(tmpl)["compute"]
    drift = RH.compute_drift(run, base, ids)
    assert drift is not None
    assert abs(drift) < 0.10, (
        "wall-capped goals are being counted: drift %.2f" % drift)


def test_nothing_comparable_is_reported_as_such():
    v = RH.assess({}, {}, TEMPLATE)
    assert v["environmental"] is False
    assert v["compute_drift"] is None
    assert "not enough goals" in v["note"]


def test_a_missing_template_does_not_crash_it():
    v = RH.assess(RUN41, RUN40, None)
    assert v["environmental"] is False
