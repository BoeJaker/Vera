"""Removing a node's own model copies once the shared store serves them.

2026-09-28: after the store took over, gpu-250 still held ~20 GB of copies -
SD 1.5 in three HF caches, Whisper base four times, Kokoro, Coqui, rembg.
The script runs for real here, over a fake node filesystem."""
import os
import subprocess

import pytest

from Vera.vera.catalog import specialist_core as sc

pytestmark = pytest.mark.critical


def _w(p, data=b"x"):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)


def _node(tmp_path):
    root = str(tmp_path / "node")
    store = str(tmp_path / "node" / "opt" / "vera-store" / "models")
    os.makedirs(os.path.join(store, "nlp"))
    # store copies
    for rel, data in (("sd/runwayml__stable-diffusion-v1-5/unet/w.safetensors", b"u" * 10),
                      ("sd/runwayml__stable-diffusion-v1-5/model_index.json", b"{}"),
                      ("whisper/base.pt", b"whisper-base"),
                      ("tts/kokoro-v1.0/kokoro-v1.0.onnx", b"kokoro"),
                      ("tts/kokoro-v1.0/voices-v1.0.bin", b"voices"),
                      ("rembg/u2net.onnx", b"u2net"),
                      ("tts/coqui/tts/tts_models--en--ljspeech--tacotron2-DDC/model_file.pth", b"c")):
        _w(os.path.join(store, rel), data)
    hub = root + "/.cache/huggingface/hub"
    # a full copy (as blobs + symlinked snapshot, like the HF cache)
    blob = hub + "/models--runwayml--stable-diffusion-v1-5/blobs/b1"
    _w(blob, b"u" * 10)
    snap = hub + "/models--runwayml--stable-diffusion-v1-5/snapshots/rev1"
    os.makedirs(snap + "/unet")
    os.symlink(blob, snap + "/unet/w.safetensors")
    _w(snap + "/model_index.json", b"{}")
    # a model the store does NOT hold
    _w(hub + "/models--lllyasviel--Annotators/snapshots/r/body.pth", b"a")
    # a same-named copy that DIFFERS from the store's
    _w(root + "/home/Servers/.cache/huggingface/hub/models--runwayml--stable-diffusion-v1-5"
                "/snapshots/r2/unet/w.safetensors", b"different-size")
    _w(root + "/.cache/whisper/base.pt", b"whisper-base")
    _w(root + "/home/Server/.cache/whisper/base.pt", b"whisper-OTHER")
    _w(root + "/home/Servers/StableDiffustionWhisper/kokoro-v1.0.onnx", b"kokoro")
    _w(root + "/home/Servers/StableDiffustionWhisper/voices-v1.0.bin", b"voices")
    _w(root + "/.u2net/u2net.onnx", b"u2net")
    _w(root + "/.u2net/u2net_human_seg.onnx", b"h")
    _w(root + "/opt/model-cache/tts/tts/tts_models--en--ljspeech--tacotron2-DDC/model_file.pth", b"c")
    return root, store


def _run(script):
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return sc.parse_prune(out.stdout)


def test_dry_run_names_exactly_the_copies_the_store_holds(tmp_path):
    root, store = _node(tmp_path)
    rep = _run(sc.node_cache_prune_script(dry_run=True, store=store, root=root))
    would = {r["path"].replace(root, "") for r in rep["rows"] if r["action"] == "would"}
    keep = {r["path"].replace(root, "") for r in rep["rows"] if r["action"] == "keep"}
    assert would == {
        "/.cache/huggingface/hub/models--runwayml--stable-diffusion-v1-5",
        "/.cache/whisper/base.pt",
        "/home/Servers/StableDiffustionWhisper/kokoro-v1.0.onnx",
        "/home/Servers/StableDiffustionWhisper/voices-v1.0.bin",
        "/.u2net/u2net.onnx",
        "/opt/model-cache/tts/tts/tts_models--en--ljspeech--tacotron2-DDC"}
    assert "/.cache/huggingface/hub/models--lllyasviel--Annotators" in keep       # not in the store
    assert "/home/Servers/.cache/huggingface/hub/models--runwayml--stable-diffusion-v1-5" in keep  # differs
    assert "/home/Server/.cache/whisper/base.pt" in keep                          # differs
    assert "/.u2net/u2net_human_seg.onnx" in keep
    # a dry run removes nothing
    assert os.path.exists(root + "/.cache/whisper/base.pt")


def test_prune_removes_them_and_never_touches_the_store(tmp_path):
    root, store = _node(tmp_path)
    before = sorted(os.path.join(d, f) for d, _s, fs in os.walk(store) for f in fs)
    rep = _run(sc.node_cache_prune_script(dry_run=False, store=store, root=root))
    assert {r["action"] for r in rep["rows"]} == {"prune", "keep"}
    assert not os.path.exists(root + "/.cache/huggingface/hub/models--runwayml--stable-diffusion-v1-5")
    assert not os.path.exists(root + "/.cache/whisper/base.pt")
    assert os.path.exists(root + "/home/Server/.cache/whisper/base.pt")
    assert os.path.exists(root + "/.cache/huggingface/hub/models--lllyasviel--Annotators")
    assert sorted(os.path.join(d, f) for d, _s, fs in os.walk(store) for f in fs) == before


def test_nothing_goes_when_the_store_is_not_mounted(tmp_path):
    root, _store = _node(tmp_path)
    rep = _run(sc.node_cache_prune_script(dry_run=False, store=str(tmp_path / "absent"), root=root))
    assert rep["aborted"] and not [r for r in rep["rows"] if r["action"] == "prune"]
    assert os.path.exists(root + "/.cache/whisper/base.pt")
