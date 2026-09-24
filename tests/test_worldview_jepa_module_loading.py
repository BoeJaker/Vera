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
assert callable(namespace["cap_worldview_retrieval_status"])
assert callable(namespace["cap_worldview_retrieval_query"])
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
state = bind_cap.__globals__
DatasetSnapshot = state["DatasetSnapshot"]

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
    last_fabric_persist = ""
    def serialize_bytes(self):
        return b"exact-checkpoint"

class Index:
    available = True
    _ids = ["r1", "r2"]

async def store(blob, meta):
    assert blob == b"exact-checkpoint"
    assert meta["retrieval_provenance"]["snapshot"]["snapshot_id"] == snapshot.snapshot_id
    return {"sqlite": True, "postgres": False, "bytes": len(blob)}

async def query(**kwargs):
    return {"results": [
        {"id": "r2", "score": 0.9, "dataset_id": "worldview-corpus",
         "text": "private beta", "concept": 1},
    ], "query": kwargs["text"]}

state["MODEL"] = Model()
state["WV_INDEX"] = Index()
state["_fabric_store_checkpoint"] = store
bound = asyncio.run(bind_cap(
    snapshot_json=json.dumps(snapshot.to_dict()),
    records_json=json.dumps(list(frozen)),
))
assert bound["ok"] is True and bound["eligible"] is True, bound
status = asyncio.run(status_cap())
assert status["eligible"] is True, status

state["cap_worldview_query"] = query
result = asyncio.run(query_cap(
    text="private query", top_k=1, snapshot_id=snapshot.snapshot_id))
assert result["ok"] is True, result
assert result["query_digest"] == "sha256:" + hashlib.sha256(b"private query").hexdigest()
assert result["citations"] == [{"record_id": "r2", "revision_id": "rev-b"}]
assert "private query" not in repr(result)
assert "private beta" not in repr(result)
assert result["activation_authority"] is False
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
