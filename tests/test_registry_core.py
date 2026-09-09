"""The registry has to survive the round trip, or it launders provenance.

The point of recording who operated a census is that the record still says
"claude" a week later. Two things can quietly destroy that: a projection into
Vera's own skill store that comes back looking Vera-native, and an update that
overwrites fields it never mentioned. Both are tested here.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vera.registry import registry_core as R                    # noqa: E402


LOOP = {
    "kind": "tool", "name": "/loop",
    "summary": "Self-paced recurring prompt; schedules the next wake-up itself.",
    "body": "Parse [interval] <prompt>. No interval means dynamic self-pacing.",
    "owner": {"agent": "claude", "session": "22e34f10"},
    "source": {"origin": "claude-code", "path": "bundled:loop"},
    "tags": ["loop", "scheduling"],
    "helpers": [{"name": "run_census.py", "purpose": "drives a census template",
                 "path": "/home/boejaker/loop-census/run_census.py"}],
}


# â”€â”€ the record â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def test_ids_are_namespaced_by_kind():
    """A tool and the loop it runs are both called 'loop' by the person using
    them; without the kind prefix one would overwrite the other."""
    assert R.entry_id("tool", "/loop") != R.entry_id("loop", "/loop")
    assert R.entry_id("tool", "/loop") == "tool:loop"


def test_normalise_never_raises_on_rubbish():
    for junk in (None, 42, "a string", [], {"kind": "nonsense"}):
        out = R.normalise(junk)
        assert set(out) >= {"id", "kind", "name", "helpers", "owner"}


def test_an_entry_with_no_summary_is_refused():
    """A registry of names is what the estate already had."""
    bad = dict(LOOP); bad.pop("summary")
    assert any("no summary" in p for p in R.problems(bad))


def test_an_entry_with_no_origin_is_refused():
    bad = dict(LOOP); bad["source"] = {}
    assert any("source.origin" in p for p in R.problems(bad))


def test_an_unknown_kind_is_named_not_silently_accepted():
    assert any("unknown kind" in p for p in R.problems(dict(LOOP, kind="widget")))


def test_a_good_entry_has_no_problems():
    assert R.problems(LOOP) == []


# â”€â”€ helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def test_a_helper_without_a_purpose_is_dropped_not_kept_empty():
    """A half-recorded helper reads as coverage. The file alone does not say
    why it existed, which is the only thing worth keeping."""
    e = R.normalise(dict(LOOP, helpers=[{"name": "x.py"},
                                        {"name": "y.py", "purpose": "counts rows"}]))
    assert [h["name"] for h in e["helpers"]] == ["y.py"]


def test_a_helper_without_a_purpose_is_reported_as_a_problem():
    assert any("has no purpose" in p
               for p in R.problems(dict(LOOP, helpers=[{"name": "x.py"}])))


# â”€â”€ merge â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def test_an_update_does_not_clear_fields_it_never_mentioned():
    merged = R.merge(LOOP, {"summary": "new summary"}, now="T2")
    assert merged["summary"] == "new summary"
    assert merged["tags"] == ["loop", "scheduling"]
    assert merged["helpers"][0]["name"] == "run_census.py"
    assert merged["owner"]["agent"] == "claude"


def test_an_edit_is_not_a_re_registration():
    """created_at is the entry's, never the update's â€” a registry that forgets
    when something first appeared cannot answer the only question a timeline
    asks."""
    first = R.normalise(LOOP, now="T1")
    merged = R.merge(first, {"summary": "edited"}, now="T2")
    assert merged["created_at"] == "T1"
    assert merged["updated_at"] == "T2"


def test_a_nested_update_merges_rather_than_replacing_the_whole_block():
    merged = R.merge(LOOP, {"source": {"commit": "abc123"}})
    assert merged["source"]["commit"] == "abc123"
    assert merged["source"]["origin"] == "claude-code"     # not wiped


# â”€â”€ search â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def test_a_name_match_outranks_a_body_match():
    """Searching 'loop' must surface the /loop tool before every entry whose
    prose happens to mention a loop."""
    other = dict(LOOP, name="census harness", kind="skill",
                 body="drives the loop repeatedly", tags=[], helpers=[])
    ranked = R.search([other, LOOP], "loop")
    assert ranked[0]["name"] == "/loop"


def test_every_term_must_land_or_the_result_set_stops_meaning_anything():
    assert R.search([LOOP], "loop scheduling") != []
    assert R.search([LOOP], "loop quantum-tunnelling") == []


def test_filters_are_anded_with_the_query():
    assert R.search([LOOP], "loop", kind="skill") == []
    assert R.search([LOOP], "loop", kind="tool") != []
    assert R.search([LOOP], "loop", owner="codex") == []
    assert R.search([LOOP], "loop", tag="scheduling") != []


def test_an_empty_query_lists_everything_rather_than_nothing():
    assert len(R.search([LOOP], "")) == 1


def test_ordering_is_stable_across_calls():
    a = dict(LOOP, name="alpha", id="tool:alpha")
    b = dict(LOOP, name="beta", id="tool:beta")
    assert [e["name"] for e in R.search([a, b], "loop")] == \
           [e["name"] for e in R.search([b, a], "loop")]


# â”€â”€ projections â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def test_the_vera_skill_projection_uses_a_type_vera_accepts():
    for kind in R.KINDS:
        t = R.to_vera_skill(dict(LOOP, kind=kind))["type"]
        assert t in R._VERA_SKILL_TYPES, (kind, t)


def test_a_round_trip_does_not_launder_a_claude_skill_into_a_vera_one():
    """THE test. Provenance surviving the projection is the whole reason the
    registry is worth having."""
    back = R.from_vera_skill(R.to_vera_skill(LOOP))
    assert back["source"]["origin"] == "claude-code"
    assert back["kind"] == "tool"
    assert back["id"] == LOOP.get("id") or back["id"] == R.entry_id("tool", "/loop")


def test_a_round_trip_keeps_the_real_tags_and_drops_the_bookkeeping_ones():
    back = R.from_vera_skill(R.to_vera_skill(LOOP))
    assert "loop" in back["tags"] and "scheduling" in back["tags"]
    assert not any(t.startswith(("registry:", "kind:", "origin:")) for t in back["tags"])


def test_a_genuinely_vera_native_skill_reads_as_vera():
    assert R.from_vera_skill({"name": "sys-cap-usage", "description": "d",
                              "content": "c", "tags": []})["source"]["origin"] == "vera"


def test_helpers_survive_into_the_skill_body_since_a_skill_has_nowhere_else():
    body = R.to_vera_skill(LOOP)["content"]
    assert "run_census.py" in body and "drives a census template" in body


def test_the_interop_projection_says_when_nothing_inside_vera_can_invoke_it():
    """An entry Vera cannot call is not broken â€” it is an external technique.
    Say which; do not imply."""
    assert R.to_interop(LOOP)["resolution"]["external_only"] is True
    reachable = R.to_interop(dict(LOOP, interop={"cap": "census.run"}))
    assert reachable["resolution"]["external_only"] is False
    assert reachable["resolution"]["reachable_as"] == ["census.run"]


def test_the_interop_projection_carries_the_evidence_boundary():
    ev = R.to_interop(LOOP)["evidence"]
    assert ev["source_path"] == "bundled:loop"
    assert ev["helpers"][0]["purpose"] == "drives a census template"


def test_an_unattributed_entry_says_so_rather_than_guessing():
    e = dict(LOOP); e.pop("owner")
    assert R.to_interop(e)["policy"]["owner"] == "unattributed"
