import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.critical


def test_worldview_panel_surfaces_retrieval_evidence_state():
    panel = (
        Path(__file__).parents[1] / "vera" / "worldview" / "worldview_panel.html"
    ).read_text(encoding="utf-8")
    assert "api('/worldview/retrieval/status'" in panel
    assert "Retrieval evidence" in panel
    assert "Checkpoint, index membership, dataset snapshot, and record revisions are pinned" in panel


def test_worldview_module_loads_as_standalone_without_torch():
    """Match the production loader: run_path gives the file no package context."""
    module = Path(__file__).parents[1] / "vera" / "worldview" / "worldview_jepa.py"
    script = r'''
import builtins
import runpy
import sys

path = sys.argv[1]
real_import = builtins.__import__
def without_torch(name, *args, **kwargs):
    if name == "torch" or name.startswith("torch."):
        raise ImportError("torch deliberately unavailable")
    return real_import(name, *args, **kwargs)
builtins.__import__ = without_torch
namespace = runpy.run_path(path, run_name="worldview_jepa_no_torch")
assert namespace["HAS_TORCH"] is False
assert namespace["MODEL"].ready is False
assert callable(namespace["cap_worldview_retrieval_bind"])
assert callable(namespace["cap_worldview_retrieval_corpus_preview"])
assert callable(namespace["cap_worldview_canonical_corpus_preview"])
assert callable(namespace["cap_worldview_canonical_corpus_materialize"])
assert callable(namespace["cap_worldview_retrieval_snapshot_preview"])
assert callable(namespace["cap_worldview_retrieval_status"])
assert callable(namespace["cap_worldview_retrieval_query"])
assert callable(namespace["cap_worldview_retrieval_rerank_canary"])
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(module)],
        cwd=module.parents[2],
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-4000:]


def test_provenance_capabilities_bind_and_redact_with_injected_runtime():
    module = Path(__file__).parents[1] / "vera" / "worldview" / "worldview_jepa.py"
    script = r'''
import asyncio
import builtins
import hashlib
import inspect
import json
import runpy
import sys

real_import = builtins.__import__
def without_torch(name, *args, **kwargs):
    if name == "torch" or name.startswith("torch."):
        raise ImportError("torch deliberately unavailable")
    return real_import(name, *args, **kwargs)
builtins.__import__ = without_torch
namespace = runpy.run_path(sys.argv[1], run_name="worldview_jepa_no_torch_caps")
bind_cap = inspect.unwrap(namespace["cap_worldview_retrieval_bind"])
status_cap = inspect.unwrap(namespace["cap_worldview_retrieval_status"])
query_cap = inspect.unwrap(namespace["cap_worldview_retrieval_query"])
rerank_cap = inspect.unwrap(namespace["cap_worldview_retrieval_rerank_canary"])
preview_cap = inspect.unwrap(namespace["cap_worldview_retrieval_snapshot_preview"])
state = bind_cap.__globals__
DatasetSnapshot = state["DatasetSnapshot"]
assert set(state["_orch"].CAPABILITY_REGISTRY[
    "worldview.retrieval.rerank.canary"]["redact_args"]) == {"text"}

records = [
    {"record_id": "r1", "revision_id": "rev-a", "text": "private alpha"},
    {"record_id": "r2", "revision_id": "rev-b", "text": "private beta"},
]
snapshot, frozen = DatasetSnapshot.create(
    dataset_id="worldview-corpus",
    created_at="2026-09-24T12:00:00Z",
    records=records,
    schema={"record_id": "string", "revision_id": "string", "text": "string"},
    provenance={"source": "test"},
)

class Model:
    ready = True
    train_steps = {"gnn": 1, "codebook": 1, "dynamics": 1}
    num_concepts = 2
    embed_dim = 3
    record_concepts = {"r1": 0, "r2": 1}
    record_meta = {
        "r1": {"revision_id": "rev-a", "content_hash": "sha256:a",
               "dataset_id": "source-a", "text": "private alpha",
               "concept": 0, "concept_label": "alpha"},
        "r2": {"revision_id": "rev-b", "content_hash": "sha256:b",
               "dataset_id": "source-b", "text": "private beta",
               "concept": 1, "concept_label": "beta"},
    }
    last_fabric_persist = ""
    def serialize_bytes(self):
        return ("exact-checkpoint:" + self.last_fabric_persist).encode()
    def save(self):
        return None
    def load_bytes(self, blob):
        prefix = b"exact-checkpoint:"
        if not blob.startswith(prefix):
            return False
        self.last_fabric_persist = blob[len(prefix):].decode()
        return True

class Index:
    available = True
    _ids = ["r1", "r2"]
    _meta = {}
    def connect(self):
        return True

stored = {}
async def store(blob, meta):
    stored["blob"] = blob
    stored["meta"] = meta
    assert blob == ("exact-checkpoint:" + state["MODEL"].last_fabric_persist).encode()
    assert meta["saved_at"] == state["MODEL"].last_fabric_persist
    assert meta["retrieval_provenance"]["snapshot"]["snapshot_id"] == snapshot.snapshot_id
    return {"sqlite": True, "postgres": False, "bytes": len(blob)}

query_args = {}
async def query(**kwargs):
    query_args.update(kwargs)
    return {"results": [
        {"id": "r2", "score": 0.9, "dataset_id": "source-b",
         "text": "private beta", "concept": 1},
    ], "query": kwargs["text"]}

state["MODEL"] = Model()
state["WV_INDEX"] = Index()
state["_fabric_store_checkpoint"] = store
preview = asyncio.run(preview_cap())
assert preview["eligible"] is True, preview
assert preview["record_count"] == 2
assert "private alpha" not in repr(preview)
assert preview["activation_authority"] is False
state["MODEL"].record_meta = {"r1": {}, "r2": {}}
incomplete = asyncio.run(preview_cap())
assert incomplete["eligible"] is False, incomplete
assert incomplete["missing"] == {"revision_id": 2, "content_hash": 2}
state["MODEL"].record_meta = Model.record_meta
bound = asyncio.run(bind_cap(
    snapshot_json=json.dumps(snapshot.to_dict()),
    records_json=json.dumps(list(frozen)),
))
assert bound["ok"] is True and bound["eligible"] is True, bound
assert state["MODEL"].last_fabric_persist
assert stored["blob"] == state["MODEL"].serialize_bytes()
status = asyncio.run(status_cap())
assert status["eligible"] is True, status
persisted = asyncio.run(state["_persist_to_fabric"]())
assert persisted["ok"] is True, persisted
assert stored["meta"]["retrieval_provenance"]["snapshot"]["snapshot_id"] == snapshot.snapshot_id
status_after_persist = asyncio.run(status_cap())
assert status_after_persist["eligible"] is True, status_after_persist

# Restart loading must prefer the exact Fabric artifact when a stale local
# checkpoint would otherwise discard a valid persisted binding.
exact_blob = stored["blob"]
exact_meta = stored["meta"]
state["_WV_RETRIEVAL_PROVENANCE"] = None
state["MODEL"].last_fabric_persist = "stale-local"
state["MODEL"].last_fabric_load = ""
async def load_meta(): return exact_meta
async def load_blob(): return exact_blob
async def no_async_work(): return None
state["_fabric_load_checkpoint_meta"] = load_meta
state["_fabric_load_checkpoint"] = load_blob
state["_load_loss_history"] = no_async_work
state["_maybe_autostart_stream"] = no_async_work
state["_restore_index_from_cached_latents"] = lambda: None
state["HAS_TORCH"] = True
state["_wv_startup_done"] = False
asyncio.run(state["_worldview_startup_load"]())
assert state["MODEL"].last_fabric_persist != "stale-local"
status_after_restart = asyncio.run(status_cap())
assert status_after_restart["eligible"] is True, status_after_restart

state["cap_worldview_query"] = query
result = asyncio.run(query_cap(
    text="private query", top_k=1, snapshot_id=snapshot.snapshot_id))
assert result["ok"] is True, result
assert query_args == {"text": "private query", "top_k": 1}, query_args
assert result["query_digest"] == "sha256:" + hashlib.sha256(b"private query").hexdigest()
assert result["citations"] == [{"record_id": "r2", "revision_id": "rev-b"}]
assert "private query" not in repr(result)
assert "private beta" not in repr(result)
assert result["activation_authority"] is False

async def embed(_text): return [0.1, 0.2, 0.3]
state["_wv_embed"] = embed
state["MODEL"].encode_isolated = lambda _emb: [0.2, 0.3, 0.4]
state["WV_INDEX"].score_subset = lambda _latent, ids: [
    (rid, {"r1": -0.8, "r2": 0.9}[rid]) for rid in ids]
canary = asyncio.run(rerank_cap(
    text="private query",
    candidates=[{"id": "r1", "baseline_score": 0.9},
                {"id": "r2", "baseline_score": 0.1}],
    snapshot_id=snapshot.snapshot_id, weight=0.8))
assert canary["ok"] is True, canary
assert canary["mode"] == "shadow"
assert canary["authoritative"] is False
assert canary["changes_context_selection"] is False
assert canary["effect"] == "none"
assert canary["activation_authority"] is False
assert canary["changed_positions"] == 2
assert {row["revision_id"] for row in canary["candidates"]} == {"rev-a", "rev-b"}
assert "private query" not in repr(canary)
assert "private alpha" not in repr(canary)
outside = asyncio.run(rerank_cap(
    text="private query",
    candidates=[{"id": "outside", "baseline_score": 0.5}],
    snapshot_id=snapshot.snapshot_id))
assert outside["error_code"] == "candidate_outside_snapshot", outside
duplicate = asyncio.run(rerank_cap(
    text="private query",
    candidates=[{"id": "r1", "baseline_score": 0.5},
                {"id": "r1", "baseline_score": 0.4}],
    snapshot_id=snapshot.snapshot_id))
assert duplicate["error_code"] == "invalid_candidates", duplicate
mismatch = asyncio.run(rerank_cap(
    text="private query",
    candidates=[{"id": "r1", "baseline_score": 0.5}],
    snapshot_id="snap_" + "0" * 64))
assert mismatch["error_code"] == "snapshot_mismatch", mismatch
state["WV_INDEX"].score_subset = lambda _latent, _ids: []
index_drift = asyncio.run(rerank_cap(
    text="private query",
    candidates=[{"id": "r1", "baseline_score": 0.5}],
    snapshot_id=snapshot.snapshot_id))
assert index_drift["error_code"] == "candidate_index_mismatch", index_drift
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(module)],
        cwd=module.parents[2],
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-4000:]


def test_canonical_corpus_capabilities_are_preview_first_and_digest_bound():
    module = Path(__file__).parents[1] / "vera" / "worldview" / "worldview_jepa.py"
    script = r'''
import asyncio
import builtins
import inspect
import runpy
import sys
import tempfile

real_import = builtins.__import__
def without_torch(name, *args, **kwargs):
    if name == "torch" or name.startswith("torch."):
        raise ImportError("torch deliberately unavailable")
    return real_import(name, *args, **kwargs)
builtins.__import__ = without_torch
namespace = runpy.run_path(sys.argv[1], run_name="worldview_jepa_corpus_caps")
preview_cap = inspect.unwrap(namespace["cap_worldview_canonical_corpus_preview"])
materialize_cap = inspect.unwrap(namespace["cap_worldview_canonical_corpus_materialize"])
state = preview_cap.__globals__
from Vera.vera.fabric.revision_path import RevisionPath
from Vera.vera.fabric.revision_store import RevisionStore

class Collection:
    def __init__(self): self.writes = []
    def get(self, **kwargs):
        assert kwargs["where"] == {"dataset_id": {"$eq": "legacy.source"}}
        return {
            "ids": ["legacy-1"], "documents": ["private text"],
            "metadatas": [{"created_at": "2026-10-01T10:00:00Z", "tags": "[]"}],
            "embeddings": [[0.1] * 12],
        }
    def upsert(self, **kwargs): self.writes.append(kwargs)

class Chroma:
    available = True
    def __init__(self): self._col = Collection()

class Fabric:
    def __init__(self):
        self.FABRIC_CHROMA = Chroma()
        self.path = RevisionPath(RevisionStore(tempfile.mktemp()),
                                 lambda action, actor, resource: True)
    def _canonical_revision_path(self): return self.path
    def _revision_actor(self): return "codex"

fabric = Fabric()
state["_get_fabric"] = lambda: fabric
preview = asyncio.run(preview_cap(
    source_dataset_id="legacy.source",
    target_dataset_id="jepa.canonical.source", limit=200))
assert preview["ok"] is True and preview["eligible_records"] == 1, preview
assert preview["mutates"] is False
assert "private text" not in repr(preview)
dry = asyncio.run(materialize_cap(
    source_dataset_id="legacy.source",
    target_dataset_id="jepa.canonical.source", limit=200, apply=False))
assert dry["requires_apply"] is True and fabric.FABRIC_CHROMA._col.writes == [], dry
stale = asyncio.run(materialize_cap(
    source_dataset_id="legacy.source",
    target_dataset_id="jepa.canonical.source", limit=200,
    expected_plan_digest="sha256:" + "0" * 64, apply=True))
assert stale["ok"] is False and fabric.FABRIC_CHROMA._col.writes == [], stale
applied = asyncio.run(materialize_cap(
    source_dataset_id="legacy.source",
    target_dataset_id="jepa.canonical.source", limit=200,
    expected_plan_digest=preview["plan_digest"], apply=True))
assert applied["ok"] is True and applied["applied"] == 1, applied
assert len(fabric.FABRIC_CHROMA._col.writes) == 1
metadata = fabric.FABRIC_CHROMA._col.writes[0]["metadatas"][0]
assert metadata["revision_id"].startswith("rev_")
assert metadata["content_hash"].startswith("sha256:")
assert applied["activation_authority"] is False
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(module)],
        cwd=module.parents[2], text=True, capture_output=True,
        timeout=90, check=False,
    )
    assert result.returncode == 0, result.stderr[-4000:]
