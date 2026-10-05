"""vLLM instance registry: runtime add/remove survives a restart, merged over the env list."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vera.vllm import vllm_registry_core as rc  # noqa: E402

pytestmark = pytest.mark.critical

ENV = [
    {"id": "vllm-gpu-251", "url": "http://192.168.0.251:8000/", "has_gpu": True,
     "priority": 0, "api_key": "k-env"},
    {"id": "vllm-cpu-252", "url": "http://192.168.0.252:8000", "has_gpu": False,
     "priority": 1},
]


def _ids(specs):
    return [s["id"] for s in specs]


def test_env_only_when_nothing_stored():
    out = rc.merge(ENV, None)
    assert _ids(out) == ["vllm-gpu-251", "vllm-cpu-252"]


def test_runtime_add_is_restored():
    doc = rc.note_added(None, "vllm-cpu-253", {"url": "http://192.168.0.253:8000/",
                                               "has_gpu": False, "priority": 2})
    out = {s["id"]: s for s in rc.merge(ENV, doc)}
    assert out["vllm-cpu-253"]["url"] == "http://192.168.0.253:8000"
    assert out["vllm-cpu-253"]["has_gpu"] is False


def test_stored_overrides_env_but_keeps_env_api_key():
    doc = rc.note_added(None, "vllm-gpu-251", {"url": "http://10.0.0.9:8000",
                                               "has_gpu": True, "label": "moved"})
    out = {s["id"]: s for s in rc.merge(ENV, doc)}
    assert out["vllm-gpu-251"]["url"] == "http://10.0.0.9:8000"
    assert out["vllm-gpu-251"]["api_key"] == "k-env"


def test_removing_an_env_instance_leaves_a_tombstone():
    doc = rc.note_removed(None, "vllm-cpu-252", env_ids=_ids(ENV))
    assert doc["removed"] == ["vllm-cpu-252"]
    assert _ids(rc.merge(ENV, doc)) == ["vllm-gpu-251"]


def test_removing_a_runtime_instance_needs_no_tombstone():
    doc = rc.note_added(None, "x", {"url": "http://h:1"})
    doc = rc.note_removed(doc, "x", env_ids=_ids(ENV))
    assert doc == {"instances": {}, "removed": []}


def test_re_adding_clears_the_tombstone():
    doc = rc.note_removed(None, "vllm-cpu-252", env_ids=_ids(ENV))
    doc = rc.note_added(doc, "vllm-cpu-252", {"url": "http://192.168.0.252:8000",
                                              "has_gpu": False})
    assert "vllm-cpu-252" in _ids(rc.merge(ENV, doc))
    assert doc["removed"] == []


def test_api_keys_are_never_persisted():
    doc = rc.note_added(None, "a", {"url": "http://h:1", "api_key": "secret"})
    assert "api_key" not in doc["instances"]["a"]
    assert set(doc["instances"]["a"]) == set(rc.PERSISTED_FIELDS)


def test_garbage_document_is_ignored():
    assert rc.normalise_doc("nope") == rc.empty_doc()
    assert rc.normalise_doc({"instances": {"a": {"label": "no url"}}}) == rc.empty_doc()
