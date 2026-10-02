"""Regression tests for WorldView defects fixed in worldview_jepa.py.

Runs the module the way production loads it (runpy, no package context) with
torch blocked, in a subprocess, matching test_worldview_jepa_module_loading.
Only torch-free logic is exercised here.
"""
import subprocess
import sys
from pathlib import Path


MODULE = Path(__file__).parents[1] / "vera" / "worldview" / "worldview_jepa.py"
PANEL = Path(__file__).parents[1] / "vera" / "vector browser" / "vector_browser_panel.html"

_PRELUDE = r'''
import asyncio
import builtins
import inspect
import random
import runpy
import sys

real_import = builtins.__import__
def without_torch(name, *args, **kwargs):
    if name == "torch" or name.startswith("torch."):
        raise ImportError("torch deliberately unavailable")
    return real_import(name, *args, **kwargs)
builtins.__import__ = without_torch
ns = runpy.run_path(sys.argv[1], run_name="worldview_jepa_fix_tests")
'''


def _run(body: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PRELUDE + body, str(MODULE)],
        cwd=MODULE.parents[2],
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-4000:]


def test_chunk_ids_covers_every_id_in_order():
    _run(r'''
chunk = ns["_chunk_ids"]
ids = [f"r{i}" for i in range(200)]
batches = chunk(ids, 64)
assert [len(b) for b in batches] == [64, 64, 64, 8]
assert [x for b in batches for x in b] == ids
assert chunk([], 64) == []
assert chunk(["a", "b"], 0) == [["a"], ["b"]]
''')


def test_stream_walk_sampling_does_not_mutate_transition_counts():
    _run(r'''
sample = ns["_stream_sample_walks"]
counts = {(1, 2): 5, (2, 3): 1, (3, 1): 2, (4, 4): 0}
before = dict(counts)
walks = sample([1, 2], [1, 2, 3, 4], counts, walk_len=6,
               rng=random.Random(0))
assert walks, "expected some walks"
assert counts == before, "sampling must not feed walks back into counts"
NS = ns["NUM_SPECIAL_TOKENS"]
for w in walks:
    assert w[0] == ns["TOKEN_BOS"]
    assert len(w) == 1 + 6
    body = [t - NS for t in w[1:]]
    assert body[0] in (1, 2)
    for a, b in zip(body, body[1:]):
        if a in (1, 2, 3):          # has outgoing evidence: must follow it
            assert counts.get((a, b), 0) > 0, (a, b)
assert sample([], [1], counts, walk_len=4) == []
''')


def test_stream_dynamics_update_and_train_do_not_write_synthetic_transitions():
    src = MODULE.read_text(encoding="utf-8")
    start = src.index("async def _stream_dynamics_update")
    end = src.index("async def _wv_stream_worker")
    assert "transition_counts[" not in src[start:end]
    # train no longer adds pairs from consecutive graph["rids"] (Chroma order)
    t0 = src.index("async def cap_worldview_train(")
    t1 = src.index("async def cap_worldview_train_stage(")
    assert "transition_counts[" not in src[t0:t1]


def test_stream_worker_encodes_all_record_ids_before_ack():
    src = MODULE.read_text(encoding="utf-8")
    start = src.index("async def _wv_stream_worker")
    body = src[start:src.index("@capability", start)]
    assert "_chunk_ids(" in body
    assert body.index("_chunk_ids(") < body.index("await redis.xack(")


def test_clear_stale_concept_labels():
    _run(r'''
class M: pass
m = M()
m.concept_labels = {1: "a", 2: "b"}
assert ns["_clear_stale_concept_labels"](m) == 2
assert m.concept_labels == {}
''')


def test_config_set_rejects_architecture_keys():
    _run(r'''
cap = inspect.unwrap(ns["cap_worldview_config_set"])
g = cap.__globals__
async def _no_emit(*a, **k):
    return None
g["_emit"] = _no_emit
cfg = g["_WV_CONFIG"]
before_k = cfg["num_concepts"]
before_lat = cfg["latent_dim"]
out = asyncio.run(cap(num_concepts=64, latent_dim=32, walk_len=9,
                      target_ema_decay=0.9))
assert cfg["num_concepts"] == before_k
assert cfg["latent_dim"] == before_lat
assert cfg["walk_len"] == 9
assert [u["key"] for u in out["updated"]] == ["walk_len"]
rej = {r["key"]: r for r in out["rejected"]}
assert set(rej) == {"num_concepts", "latent_dim", "target_ema_decay"}
assert rej["num_concepts"]["env"] == "WORLDVIEW_NUM_CONCEPTS"
assert rej["target_ema_decay"]["env"] is None
assert "num_concepts" in out["warning"] and "restart" in out["warning"]
out2 = asyncio.run(cap(walk_len=10))
assert "rejected" not in out2 and "warning" not in out2
# Every arch key is a real config key, and none is read from _WV_CONFIG.
for k in g["_WV_ARCH_KEYS"]:
    assert k in cfg
''')


def test_counterfactual_validates_swap_at_without_model():
    _run(r'''
cap = inspect.unwrap(ns["cap_worldview_counterfactual"])
sig = inspect.signature(cap)
assert "seed" in sig.parameters
out = asyncio.run(cap(start_concept=1, swap_to=2))
assert out == {"error": "WorldView not ready"}
''')


def test_diagnose_capability_exists_and_panel_uses_it():
    src = MODULE.read_text(encoding="utf-8")
    assert '"worldview.diagnose"' in src
    assert 'http_method="POST", http_path="/worldview/diagnose"' in src
    panel = PANEL.read_text(encoding="utf-8")
    assert "api('/worldview/diagnose', {})" in panel     # POST with a body
    assert "probe.probe_dim" in panel
    _run(r'''
cap = inspect.unwrap(ns["cap_worldview_diagnose"])
g = cap.__globals__
g["_get_fabric"] = lambda: None
out = asyncio.run(cap(probe=False))
assert out["fabric_module_loaded"] is False
assert out["probe_dim"] is None
assert "model_embed_dim" in out and out["hint"]
''')


def test_summarise_description_does_not_promise_anomalies():
    src = MODULE.read_text(encoding="utf-8")
    start = src.index('"worldview.summarise"')
    desc = src[start:src.index("async def cap_worldview_summarise")]
    assert "recent anomalies" not in desc
