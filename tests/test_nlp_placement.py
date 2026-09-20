"""NLP must run off the Vera host, and the switch that says so must mean it.

Two failures this guards, both of which the subsystem was designed around:

1. A SILENT FALLBACK TO THE HOST. `nlp.local` exists to keep CPU-bound NLP off
   a 2-core VM whose event loop stalling takes the whole system down. A switch
   that falls back to in-process execution when no node answers is worse than
   no switch: the host burns exactly as before while the operator believes the
   work moved. So "switch off, nothing available" must be an ERROR, never a
   local run.

2. ROUTING ON `loadavg`. gpu-250 / cpu-246 / cpu-247 are LXC containers on ONE
   Proxmox host and share a kernel, so all three report the SAME load figure at
   any instant — measured repeatedly on 2026-09-20. loadavg is the obvious
   signal and everyone reaches for it; it would appear to work while choosing
   at random. The test below gives three nodes an identical (and enormous)
   loadavg and requires the choice to be driven by signals that actually
   differ between containers.

Pure: dicts in, decisions out.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.research import nlp_dispatch_core as C      # noqa: E402

pytestmark = pytest.mark.critical


# ── The switch ───────────────────────────────────────────────────────────────

def test_switch_off_and_no_node_is_an_error_not_a_local_run():
    """The whole point of the switch. Must never resolve to LOCAL."""
    out = C.resolve_placement(nlp_local=False, servers=[])
    assert out["where"] == C.FAIL
    assert out["where"] != C.LOCAL
    # The error has to name the control, or nobody can act on it.
    assert "nlp.local" in out["reason"]


def test_switch_on_and_no_node_permits_the_host():
    out = C.resolve_placement(nlp_local=True, servers=[])
    assert out["where"] == C.LOCAL


def test_a_node_is_preferred_even_when_the_host_is_allowed():
    """nlp.local is permission to use the host, not a preference for it."""
    out = C.resolve_placement(nlp_local=True, servers=[{"node_id": "gpu-250"}])
    assert out["where"] == C.REMOTE


def test_a_node_serves_while_the_switch_is_off():
    out = C.resolve_placement(nlp_local=False, servers=[{"node_id": "gpu-250"}])
    assert out["where"] == C.REMOTE


# ── Node choice ──────────────────────────────────────────────────────────────

def _node(nid, runners=0, avail=50000, total=51200, gpu_util=None):
    n = {"node_id": nid, "runners": runners,
         "mem_available_mb": avail, "mem_total_mb": total,
         # Identical on every node, and enormous: if this is ever read, the
         # assertions below stop holding.
         "load": [18.82, 15.92, 15.69]}
    if gpu_util is not None:
        n["gpu"] = {"name": "Tesla V100-PCIE-12GB", "util_pct": gpu_util}
    return n


def test_loadavg_is_not_used_to_choose():
    """Identical loadavg on all three — the choice must still discriminate."""
    busy = _node("cpu-246", runners=4, avail=20000)
    idle = _node("cpu-247", runners=0, avail=50000)
    picked, why = C.pick_nlp_node([busy, idle])
    assert picked["node_id"] == "cpu-247"
    assert "cpu-247" in why


def test_runner_count_beats_a_tie():
    a = _node("cpu-246", runners=1)
    b = _node("cpu-247", runners=3)
    picked, _ = C.pick_nlp_node([a, b])
    assert picked["node_id"] == "cpu-246"


def test_memory_headroom_discriminates():
    roomy = _node("cpu-246", runners=1, avail=49000)
    tight = _node("cpu-247", runners=1, avail=2000)
    picked, _ = C.pick_nlp_node([roomy, tight])
    assert picked["node_id"] == "cpu-246"


def test_a_busy_gpu_makes_a_node_more_attractive_not_less():
    """GPU busy means ollama is decoding on the V100 and NOT on the twelve
    cores — the one combination where NLP on that container is genuinely free.
    """
    gpu_busy = _node("gpu-250", runners=1, gpu_util=95)
    cpu_only = _node("cpu-246", runners=1)
    assert C.score_nlp_node(gpu_busy) < C.score_nlp_node(cpu_only)
    picked, _ = C.pick_nlp_node([cpu_only, gpu_busy])
    assert picked["node_id"] == "gpu-250"


def test_no_candidates_is_reported_not_guessed():
    picked, why = C.pick_nlp_node([])
    assert picked is None
    assert why


def test_choice_is_deterministic_on_a_tie():
    a, b = _node("cpu-247"), _node("cpu-246")
    assert C.pick_nlp_node([a, b])[0]["node_id"] == "cpu-246"
    assert C.pick_nlp_node([b, a])[0]["node_id"] == "cpu-246"


# ── Chunking ─────────────────────────────────────────────────────────────────

def test_the_whole_document_is_covered_not_truncated():
    """nlp.ner used to do `text[:1024]` and report success on a document."""
    text = " ".join(f"word{i}" for i in range(2000))
    chunks = C.chunk_text(text, max_chars=200, overlap=0)
    assert len(chunks) > 1
    assert "".join(c for _o, c in chunks) == text
    for offset, chunk in chunks:
        assert text[offset:offset + len(chunk)] == chunk


def test_offsets_index_the_original_text_with_overlap():
    text = " ".join(f"word{i}" for i in range(500))
    for offset, chunk in C.chunk_text(text, max_chars=150, overlap=40):
        assert text[offset:offset + len(chunk)] == chunk


def test_a_large_overlap_still_terminates():
    text = "x" * 5000
    chunks = C.chunk_text(text, max_chars=100, overlap=99)
    assert chunks
    assert chunks[-1][0] + len(chunks[-1][1]) == len(text)


def test_empty_text_is_no_chunks():
    assert C.chunk_text("") == []


def test_bad_chunk_arguments_raise():
    with pytest.raises(ValueError):
        C.chunk_text("abc", max_chars=0)
    with pytest.raises(ValueError):
        C.chunk_text("abc", max_chars=10, overlap=10)


# ── Merging ──────────────────────────────────────────────────────────────────

def test_entity_offsets_are_rebased_onto_the_document():
    pieces = [
        (0,   [{"entity": "PERSON", "word": "Alice", "score": 0.99,
                "start": 0, "end": 5}]),
        (100, [{"entity": "DATE", "word": "March", "score": 0.95,
                "start": 10, "end": 15}]),
    ]
    merged = C.merge_chunk_entities(pieces)
    assert [(e["start"], e["end"]) for e in merged] == [(0, 5), (110, 115)]


def test_an_entity_seen_twice_in_an_overlap_appears_once():
    ent = {"entity": "PERSON", "word": "Alice", "start": 10, "end": 15}
    pieces = [(0, [{**ent, "score": 0.80}]),
              (0, [{**ent, "score": 0.97}])]
    merged = C.merge_chunk_entities(pieces)
    assert len(merged) == 1
    assert merged[0]["score"] == 0.97        # the better sighting wins


def test_merged_entities_come_back_in_document_order():
    pieces = [(200, [{"entity": "ORG", "start": 0, "end": 3, "score": 0.9}]),
              (0,   [{"entity": "GPE", "start": 5, "end": 9, "score": 0.9}])]
    merged = C.merge_chunk_entities(pieces)
    assert [e["start"] for e in merged] == [5, 200]


# ── The model registry ───────────────────────────────────────────────────────

def test_every_model_has_a_kind():
    """The exporter picks an ORT class from TASK_KIND and the server picks a
    pipeline from it. A task in one map and not the other exports nothing, or
    loads nothing, without saying so."""
    assert set(C.DEFAULT_MODELS) == set(C.TASK_KIND), (
        "DEFAULT_MODELS and TASK_KIND disagree: "
        f"{set(C.DEFAULT_MODELS) ^ set(C.TASK_KIND)}")


def test_slug_is_stable_and_filesystem_safe():
    """The slug IS the directory name in the shared store. If it ever changes,
    every previously exported model becomes invisible to the server — which on
    a read-only store means falling back to a hub download that cannot write."""
    assert C.model_slug("djagatiya/ner-roberta-base-ontonotesv5-englishv4") == \
        "djagatiya__ner-roberta-base-ontonotesv5-englishv4"
    for model_id in C.DEFAULT_MODELS.values():
        slug = C.model_slug(model_id)
        assert "/" not in slug and "\\" not in slug
        assert slug and slug == C.model_slug(model_id)      # deterministic


def test_ner_default_is_an_ontonotes_model():
    """The DATE label is the whole reason for this model choice."""
    assert "ontonotes" in C.DEFAULT_MODELS["ner"].lower()


def test_ontonotes_date_survives_the_merge():
    """The label set is the reason for the model change — guard it end to end."""
    pieces = [(0, [{"entity": "DATE", "word": "3 March 2024",
                    "score": 0.98, "start": 40, "end": 52}])]
    merged = C.merge_chunk_entities(pieces)
    assert merged[0]["entity"] == "DATE"
