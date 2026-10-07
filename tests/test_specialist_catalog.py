"""The specialist-model catalog and the builder that fills the shared store.

Imported lowercase with the worktree on sys.path so the worktree copy is tested."""
import importlib.util
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from vera.catalog import specialist_catalog as cat  # noqa: E402
from vera.catalog import specialist_core as sc  # noqa: E402
from vera.research.nlp_dispatch_core import DEFAULT_MODELS  # noqa: E402

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


# ── the catalog says only what is true ────────────────────────────────────────
def test_in_use_is_exactly_what_the_nodes_serve():
    # "in use" is a claim about production: for NLP it must be the registry the
    # nodes load, no more and no less
    nlp_in_use = {(e["task"], e["model"]) for e in cat.curated("nlp") if e["in_use"]}
    assert nlp_in_use == set(DEFAULT_MODELS.items())
    # one model in use per task
    tasks = [t for t, _m in nlp_in_use]
    assert len(tasks) == len(set(tasks))


def test_entry_ids_are_unique_and_found():
    ids = [e["id"] for e in cat.curated()]
    assert len(ids) == len(set(ids))
    for i in ids:
        assert cat.find(i)["id"] == i
    assert cat.find("nlp:ner:nope/nope") is None


def test_every_entry_becomes_a_builder_job():
    for e in cat.curated():
        job = cat.job_for(e)
        assert job["model"] == e["model"]
        assert job["kind"] in ("nlp_export", "hf_snapshot", "whisper", "url_fetch")
    assert cat.job_for(cat.find("nlp:ner:dslim/bert-base-NER")) == \
        {"kind": "nlp_export", "model": "dslim/bert-base-NER", "task": "ner"}
    sd = cat.job_for(cat.find("sd:runwayml/stable-diffusion-v1-5"))
    # the 4 GB .ckpt and the non-EMA weights are not what gpu_inference loads
    assert sd["kind"] == "hf_snapshot" and "*.ckpt" in sd["ignore_patterns"]
    tts = cat.job_for(cat.find("tts:kokoro-v1.0"))
    assert tts["kind"] == "url_fetch" and all(u.startswith("https://github.com/") for u in tts["urls"])


def test_hugging_face_picks_are_validated_and_flagged():
    e = cat.hf_pick("nlp", "someone/some-ner", task="ner")
    assert e["vetted"] is False and e["id"] == "nlp:ner:someone/some-ner"
    assert cat.job_for(e)["kind"] == "nlp_export"
    for bad in [("nlp", "someone/x", ""),            # nlp needs a task
                ("nlp", "not a repo", "ner"),
                ("nlp", "../../etc/passwd", "ner"),
                ("tts", "a/b", ""),                   # TTS files are curated URLs only
                ("whisper", "a/b", "")]:
        with pytest.raises(ValueError):
            cat.hf_pick(*bad)


def test_search_params_per_family():
    assert cat.search_params("nlp", task="zeroshot")["pipeline_tag"] == "zero-shot-classification"
    assert cat.search_params("sd")["library"] == "diffusers"
    assert cat.search_params("gliner")["library"] == "gliner"
    assert cat.search_params("nlp", task="ner", limit=500)["limit"] == 50
    with pytest.raises(ValueError):
        cat.search_params("whisper")


def test_mark_built_reads_the_store_inventory():
    store = {"families": {"nlp": [{"name": "dslim__bert-base-NER"}, {"name": "_fastembed"}],
                          "sd": [{"name": "runwayml__stable-diffusion-v1-5"}]},
             "whisper_files": ["base.pt"]}
    rows = {e["id"]: e for e in cat.mark_built(cat.curated(), store)}
    assert rows["nlp:ner:dslim/bert-base-NER"]["built"] is True
    assert rows["nlp:rerank:Xenova/ms-marco-MiniLM-L-6-v2"]["built"] is True
    assert rows["whisper:base"]["built"] is True and rows["whisper:small"]["built"] is False
    assert rows["sd:runwayml/stable-diffusion-v1-5"]["built"] is True
    assert rows["gliner:urchade/gliner_small-v2.1"]["built"] is False
    # an unreadable store marks nothing built rather than guessing
    assert not any(e["built"] for e in cat.mark_built(cat.curated(), {}))


# ── models in a node's own caches ─────────────────────────────────────────────
def test_node_cache_rows_group_copies():
    out = ("hf\t3900\t/home/Servers/.cache/huggingface/hub/models--runwayml--stable-diffusion-v1-5\n"
           "hf\t5900\t/.cache/huggingface/hub/models--runwayml--stable-diffusion-v1-5\n"
           "hf\t2400\t/.cache/huggingface/hub/models--h94--IP-Adapter\n"
           "whisper\t145\t/.cache/whisper/base.pt\n"
           "whisper\t145\t/home/Server/.cache/whisper/base.pt\n"
           "coqui\t113\t/opt/model-cache/tts/tts/tts_models--en--ljspeech--tacotron2-DDC\n"
           "garbage line\n")
    rows = {(r["kind"], r["model"]): r for r in sc.parse_node_cache(out)}
    sd = rows[("hf", "runwayml/stable-diffusion-v1-5")]
    assert sd["size_mb"] == 9800 and len(sd["paths"]) == 2
    assert rows[("whisper", "base")]["size_mb"] == 290
    assert ("coqui", "en/ljspeech/tacotron2-DDC") in rows
    assert len(rows) == 4


def test_node_cache_probe_covers_the_caches_found_on_gpu_250():
    cmd = sc.node_cache_probe_cmd()
    for p in ("/.cache/huggingface/hub", "/home/*/.cache/huggingface/hub", "/.cache/whisper",
              "/home/*/*/cache/whisper", "/.u2net", "/opt/model-cache/tts/tts",
              "kokoro-v1.0.onnx"):
        assert p in cmd, p
    assert cmd.rstrip().endswith("true")         # an empty glob is not a failure


# ── the builder ───────────────────────────────────────────────────────────────
def _builder(tmp_path, monkeypatch):
    monkeypatch.setenv("VERA_STORE_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "model_builder_under_test", os.path.join(_ROOT, "edge", "model_builder.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_builder_refuses_what_it_must_not_write(tmp_path, monkeypatch):
    mb = _builder(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        mb.submit({"kind": "hf_snapshot", "family": "ollama", "model": "a/b"})   # never ollama's blobs
    with pytest.raises(ValueError):
        mb.submit({"kind": "shell", "model": "x"})
    with pytest.raises(ValueError):
        mb.submit({"kind": "nlp_export", "model": ""})
    job = mb.submit({"kind": "nlp_export", "model": "dslim/bert-base-NER", "task": "ner"})
    assert job["family"] == "nlp" and job["state"] == "queued"


def test_a_partial_export_keeps_every_other_model_in_the_manifest(tmp_path, monkeypatch):
    mb = _builder(tmp_path, monkeypatch)
    (tmp_path / "nlp").mkdir()
    before = {"schema": "vera.nlp-export-manifest/v2",
              "models": [{"task": "ner", "model": "m/ner", "status": "ok", "dir": "m__ner"},
                         {"task": "qa", "model": "m/qa", "status": "ok", "dir": "m__qa"}],
              "model_packages": {"ner": {"version": "content-a"}, "qa": {"version": "content-b"}},
              "package_errors": {}}
    (tmp_path / "nlp" / "manifest.json").write_text(json.dumps(before))
    # an alternative NER model: added, and the registry package for ner untouched
    mb._merge_nlp_manifest({"task": "ner", "model": "alt/ner", "status": "ok", "dir": "alt__ner"},
                           None, "")
    m = json.loads((tmp_path / "nlp" / "manifest.json").read_text())
    assert {(r["task"], r["model"]) for r in m["models"]} == \
        {("ner", "m/ner"), ("qa", "m/qa"), ("ner", "alt/ner")}
    assert m["model_packages"] == before["model_packages"]
    # rebuilding the registry model replaces its entry and its package
    mb._merge_nlp_manifest({"task": "qa", "model": "m/qa", "status": "ok", "dir": "m__qa"},
                           {"version": "content-c"}, "")
    m = json.loads((tmp_path / "nlp" / "manifest.json").read_text())
    assert len([r for r in m["models"] if r["task"] == "qa"]) == 1
    assert m["model_packages"]["qa"] == {"version": "content-c"}
    assert m["model_packages"]["ner"] == {"version": "content-a"}


def test_store_inventory_lists_families_and_whisper_files(tmp_path, monkeypatch):
    mb = _builder(tmp_path, monkeypatch)
    (tmp_path / "nlp" / "m__ner").mkdir(parents=True)
    (tmp_path / "nlp" / "m__ner" / "model.onnx").write_bytes(b"x" * 10)
    (tmp_path / "whisper").mkdir()
    (tmp_path / "whisper" / "base.pt").write_bytes(b"x")
    inv = mb.store_inventory()
    assert inv["families"]["nlp"][0]["model"] == "m/ner" and inv["families"]["nlp"][0]["onnx"]
    assert inv["whisper_files"] == ["base.pt"]
